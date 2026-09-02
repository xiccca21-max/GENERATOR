"""
T-BANK STEALTH v3 — байт-уровневая правка content stream + полные шрифты.

Используется unlocked-шаблон, в котором заранее подменены сабсетные шрифты на
полные TinkoffSans-Regular/Medium и расширена ToUnicode CMap. Благодаря этому в
PDF можно вписать ЛЮБОЙ символ, поддерживаемый шрифтом (вся кириллица в обоих
регистрах, латиница, цифры, пунктуация и т.д.).

ВАЖНО: чтобы валидатор пропускал структурную проверку, мы трогаем ТОЛЬКО те
Tj-операторы, у которых есть "своя" пара (date / amount / sender / receiver /
bank / card). Tj со статусом ("Успешно"), комиссией ("Без комиссии") и
номером квитанции остаются в потоке БАЙТ-В-БАЙТ оригинальными — это и даёт
проход структурной проверки.

Базовый шаблон — `templates/T_original_unlocked.pdf` (донор: реальный чек со
статусом "Успешно"). Если unlocked-шаблон отсутствует, он создаётся через
`tbank_unlock_template.build_unlocked_template()`.
"""

import os
import re
import zlib
import logging
from time_msk import now_msk
from typing import Dict, List, Optional, Tuple

from fontTools.ttLib import TTFont

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
ORIG_TEMPLATE = os.path.join(_DIR, "templates", "T_original.pdf")
UNLOCKED_TEMPLATE = os.path.join(_DIR, "templates", "T_original_unlocked.pdf")
FONT_REGULAR = os.path.join(_DIR, "fonts", "TinkoffSans-Regular.ttf")
FONT_MEDIUM = os.path.join(_DIR, "fonts", "TinkoffSans-Medium.ttf")

_ESC = {
    0x28: b"\\(", 0x29: b"\\)", 0x5C: b"\\\\",
    0x0A: b"\\n", 0x0D: b"\\r",
    0x08: b"\\b", 0x0C: b"\\f", 0x09: b"\\t",
}

_FALLBACK = {
    "«": '"', "»": '"', "“": '"', "”": '"', "„": '"',
    "‘": "'", "’": "'", "‚": "'",
    "–": "-", "—": "-", "−": "-",
    "…": "...",
    "\u00A0": " ", "\u2009": " ", "\u202F": " ", "\u200B": "",
    "\t": " ", "\r": " ", "\n": " ",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "1",
    "K": "К", "M": "М", "N": "Н", "O": "О", "P": "Р", "R": "Я",
    "T": "Т", "X": "Х", "Y": "У", "W": "Ш",
    "c": "с", "e": "е", "h": "н", "i": "1", "m": "м", "o": "о",
    "p": "р", "w": "ш", "x": "х", "y": "у",
}

_CID: Dict[str, int] = {}
_CID_MED: Dict[str, int] = {}
_WIDTH: Dict[str, int] = {}
_WIDTH_MED: Dict[str, int] = {}
_UPEM_REG: int = 1000
_UPEM_MED: int = 1000


# Минимальный subset для card-метода (без латиницы) — даёт размер ~57-58 KB,
# что близко к оригиналу T_original.pdf (58899 байт).
def _card_unicodes_reg() -> set:
    s = set()
    s.update(ord(c) for c in "0123456789")
    s.update(ord(c) for c in " .,:;()*-+/№")
    s.update(range(0x0410, 0x0450))  # А-я
    s.update([0x0401, 0x0451])         # Ё ё
    s.update([0x00A0, 0x20BD, 0x2116, 0x2009, 0x202F])
    return s


def _card_unicodes_med() -> set:
    from tbank_unlock_template import _NEEDED_UNICODES_MED
    return _NEEDED_UNICODES_MED


def _ensure_unlocked_template() -> None:
    if os.path.exists(UNLOCKED_TEMPLATE):
        return
    logger.info("Unlocked template missing — building from original (minimal subset)")
    from tbank_unlock_template import build_unlocked_template
    build_unlocked_template(
        orig_path=ORIG_TEMPLATE,
        out_path=UNLOCKED_TEMPLATE,
        unicodes_reg=_card_unicodes_reg(),
        unicodes_med=_card_unicodes_med(),
    )


# Целевой размер — медиана корпуса «по карте на Сбербанк»
try:
    from tbank_corpus import median_size as _corpus_median_size
    _TARGET_PDF_SIZE = _corpus_median_size("card_sber")
except Exception:
    _TARGET_PDF_SIZE = 58899


def _pad_pdf_to_target(pdf: bytes, target: int = _TARGET_PDF_SIZE) -> bytes:
    """Обрезает хвост после %%EOF; не дописывает padding."""
    from sber_dynamic import _strip_pdf_eof_tail

    return _strip_pdf_eof_tail(pdf)


def _load_cid_maps() -> None:
    global _CID, _CID_MED, _WIDTH, _WIDTH_MED, _UPEM_REG, _UPEM_MED
    if _CID and _CID_MED and _WIDTH:
        return
    # Используем полный шрифт — GID-значения совпадают с теми,
    # что встраиваются в разблокированный шаблон (_prepare_full_font).
    from fontTools.ttLib import TTFont as _TTFont
    font_reg = _TTFont(FONT_REGULAR)
    font_med = _TTFont(FONT_MEDIUM)
    _UPEM_REG = font_reg["head"].unitsPerEm
    _UPEM_MED = font_med["head"].unitsPerEm
    cmap_r = font_reg.getBestCmap() or {}
    hmtx_r = font_reg["hmtx"].metrics
    for cp, name in cmap_r.items():
        try:
            gid = font_reg.getGlyphID(name)
            adv = hmtx_r.get(name, (0, 0))[0]
            ch = chr(cp)
            _CID[ch] = gid
            _WIDTH[ch] = adv
        except Exception:
            pass
    cmap_m = font_med.getBestCmap() or {}
    hmtx_m = font_med["hmtx"].metrics
    for cp, name in cmap_m.items():
        try:
            gid = font_med.getGlyphID(name)
            adv = hmtx_m.get(name, (0, 0))[0]
            ch = chr(cp)
            _CID_MED[ch] = gid
            _WIDTH_MED[ch] = adv
        except Exception:
            pass
    logger.info(
        f"CID maps loaded: regular={len(_CID)} medium={len(_CID_MED)} "
        f"upem={_UPEM_REG}/{_UPEM_MED}"
    )


def check_text(text: str) -> list:
    _load_cid_maps()
    missing = []
    for ch in text:
        if ch in _CID:
            continue
        sub = _FALLBACK.get(ch)
        if sub is not None and all(c in _CID for c in sub):
            continue
        missing.append(ch)
    return missing


def _enc_char(ch: str, medium: bool = False) -> bytes:
    table = _CID_MED if medium else _CID
    cid = table.get(ch)
    if cid is None:
        sub = _FALLBACK.get(ch, "?")
        out = bytearray()
        for sc in sub:
            out += _enc_char(sc, medium)
        return bytes(out)
    out = bytearray()
    for b in ((cid >> 8) & 0xFF, cid & 0xFF):
        out.extend(_ESC.get(b, bytes([b])))
    return bytes(out)


def _enc(text: str, medium: bool = False) -> bytes:
    _load_cid_maps()
    out = bytearray()
    for ch in text:
        out += _enc_char(ch, medium)
    return bytes(out)


def _fmt_date(dt: str) -> str:
    """'DD.MM.YYYY[, ]HH:MM[:SS]' → 'DD.MM.YYYY  HH:MM:SS' (двойной пробел)."""
    try:
        s = (dt or "").strip()
        if s.lower() in ("сейчас", "now", "-", "", "авто", "auto"):
                        s = now_msk().strftime("%d.%m.%Y  %H:%M:%S")
        s = s.replace(",", " ")
        m = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)", s)
        if m:
            t = m.group(1)
            d = (s[:m.start()] + s[m.end():]).strip()
        else:
            d = s
            t = ""
        import random as _rnd_v3

        def _nz() -> str:
            return f"{_rnd_v3.randint(1, 59):02d}"

        if t and t.count(":") == 1:
            t += f":{_nz()}"
        elif t and t.count(":") == 2:
            hh, mm, ss = t.split(":")
            if ss == "00":
                t = f"{hh}:{mm}:{_nz()}"
        if not t:
            t = now_msk().strftime(f"%H:%M:{_nz()}")
        if t.endswith(":00"):
            hh, mm, _ss = t.split(":")
            t = f"{hh}:{mm}:{_nz()}"
        return f"{d}  {t}"
    except Exception:
        return dt


def _find_streams(pdf: bytes):
    out = []
    pos = 0
    while True:
        s = pdf.find(b"stream", pos)
        if s < 0:
            break
        cs = s + 6
        if pdf[cs:cs+2] == b"\r\n":
            cs += 2
        elif pdf[cs:cs+1] == b"\n":
            cs += 1
        es = pdf.find(b"endstream", cs)
        if es < 0:
            break
        ce = es
        if pdf[ce-2:ce] == b"\r\n":
            ce -= 2
        elif pdf[ce-1:ce] == b"\n":
            ce -= 1
        out.append((cs, ce, pdf[cs:ce]))
        pos = es + 9
    return out


def _replace_once(blob: bytes, old: bytes, new: bytes) -> Tuple[bytes, bool]:
    if not old:
        return blob, False
    idx = blob.find(old)
    if idx < 0:
        return blob, False
    return blob[:idx] + new + blob[idx + len(old):], True


def _replace_all(blob: bytes, old: bytes, new: bytes) -> Tuple[bytes, int]:
    if not old or old not in blob:
        return blob, 0
    return blob.replace(old, new), blob.count(old)


def _receipt_num_from_line(receipt_line: str) -> str:
    from tbank_corpus import normalize_receipt_num

    m = re.search(r"1-\d{3}-\d{3}-\d{3}-\d{3}", receipt_line)
    if m:
        return m.group(0)
    return normalize_receipt_num(receipt_line.split()[-1])


def _replace_card_receipt(
    stream: bytes,
    enc_r,
    old_receipt_line: str,
    new_receipt_num: str,
    *,
    enc_r_old=None,
) -> Tuple[bytes, bool]:
    """№ квитанции — всегда 1-XXX-XXX-XXX-XXX, in-place без укорочения групп."""
    from tbank_corpus import normalize_receipt_num
    from tbank_orig_mode import replace_tj_bytes_inplace

    enc_old = enc_r_old or enc_r

    new_receipt_num = normalize_receipt_num(new_receipt_num)
    new_line = f"Квитанция  \u2116 {new_receipt_num}"
    old_num = _receipt_num_from_line(old_receipt_line)
    if old_num == new_receipt_num:
        return stream, True

    candidates = [
        old_receipt_line,
        old_receipt_line.replace("  ", " "),
        f"Квитанция \u2116 {old_num}",
        f"Квитанция  \u2116 {old_num}",
    ]
    seen: set = set()
    for old_text in candidates:
        if old_text in seen:
            continue
        seen.add(old_text)
        old_b = enc_old(old_text)
        new_b = enc_r(new_line)
        if not old_b or old_b not in stream:
            continue
        if len(old_b) == len(new_b):
            stream, ok = replace_tj_bytes_inplace(stream, old_b, new_b)
            if ok:
                logger.info(f"  receipt line: {old_num} -> {new_receipt_num}")
                return stream, True
        stream, ok = _replace_once(stream, old_b, new_b)
        if ok:
            logger.info(f"  receipt line: {old_num} -> {new_receipt_num}")
            return stream, True

    return stream, False


def _verify_card_receipt_in_pdf(pdf: bytes, receipt_num: str) -> bool:
    import fitz

    try:
        text = fitz.open(stream=pdf, filetype="pdf")[0].get_text()
    except Exception:
        return False
    return receipt_num in text


def _text_width_units(text: str, medium: bool = False) -> int:
    table = _WIDTH_MED if medium else _WIDTH
    fb_width = table.get(" ", 250)
    total = 0
    for ch in text:
        w = table.get(ch)
        if w is None:
            for sub in _FALLBACK.get(ch, "?"):
                w = table.get(sub, fb_width)
                total += w
            continue
        total += w
    return total


def _text_width_pt(text: str, font_size: float, medium: bool = False) -> float:
    upem = _UPEM_MED if medium else _UPEM_REG
    return _text_width_units(text, medium) * font_size / upem


_TM_RE = re.compile(rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm")


def _fmt_coord(v: float) -> str:
    """Format PDF coordinate: 2 decimal places, no trailing zeros — JasperReports style."""
    s = f"{v:.2f}"
    s = s.rstrip("0").rstrip(".")
    return s


def _replace_tj_with_tm(
    stream: bytes,
    old_text_b: bytes,
    new_text_b: bytes,
    old_text: str,
    new_text: str,
    font_size: float,
    medium: bool,
    occurrence: int = 1,
) -> Tuple[bytes, bool]:
    """Найти n-й `(old_text_b)Tj`, заменить байты текста и сдвинуть
    предшествующий `Tm` X-координату так, чтобы правый край остался на месте."""
    needle = b"(" + old_text_b + b")Tj"
    start = 0
    pos = -1
    for _ in range(occurrence):
        pos = stream.find(needle, start)
        if pos < 0:
            return stream, False
        start = pos + len(needle)

    look_from = max(0, pos - 200)
    region = stream[look_from:pos]
    tms = list(_TM_RE.finditer(region))
    if not tms:
        replaced = stream[:pos] + b"(" + new_text_b + b")Tj" + stream[pos + len(needle):]
        return replaced, True

    last = tms[-1]
    old_x = float(last.group(1))
    old_y = last.group(2).decode()
    old_w = _text_width_pt(old_text, font_size, medium)
    new_w = _text_width_pt(new_text, font_size, medium)
    new_x = old_x + (old_w - new_w)
    new_tm = f"1 0 0 1 {_fmt_coord(new_x)} {old_y} Tm".encode("ascii")
    tm_abs_start = look_from + last.start()
    tm_abs_end = look_from + last.end()
    new_tj = b"(" + new_text_b + b")Tj"
    out = (
        stream[:tm_abs_start]
        + new_tm
        + stream[tm_abs_end:pos]
        + new_tj
        + stream[pos + len(needle):]
    )
    return out, True


def _pad_to_compressed_size(stream: bytes, target_size: int) -> Optional[bytes]:
    """OpenPDF/Jasper compress_to_size — без padding в decoded stream."""
    from tbank_sbp_stealth import _pad_to_compressed_size as _sbp_pad
    return _sbp_pad(stream, target_size)


def _best_compress(stream: bytes) -> bytes:
    """OpenPDF/Jasper flate — НЕ Python zlib (чекеры ловят DEFLATE_PROFILE_MISMATCH)."""
    from openpdf_deflate import compress_like_jasper

    return compress_like_jasper(stream, level=6)


def _patch_pdf_metadata(pdf: bytes, date_time: str) -> bytes:
    """CreationDate + Keywords — тот же патч, что у SBP."""
    from tbank_channel_common import patch_channel_metadata

    return patch_channel_metadata(pdf, date_time)


def _patch_length_and_rebuild(
    pdf: bytearray, cs: int, ce: int, new_compressed: bytes
) -> Optional[bytes]:
    """Update stream bytes + /Length, shift xref offsets accordingly.
    Trailer/header stays byte-for-byte."""
    pdf_b = bytes(pdf)

    search_start = max(0, cs - 400)
    region = pdf_b[search_start:cs]
    matches = list(re.finditer(rb"/Length\s+(\d+)", region))
    if not matches:
        logger.error("Cannot find /Length for content stream")
        return None
    m = matches[-1]
    abs_length_pos = search_start + m.start()
    full_match = m.group(0)
    new_length_str = f"/Length {len(new_compressed)}".encode()
    length_delta = len(new_length_str) - len(full_match)

    pdf_b = pdf_b[:abs_length_pos] + new_length_str + pdf_b[abs_length_pos + len(full_match):]

    cs_n = cs + length_delta
    ce_n = ce + length_delta

    pdf_b = pdf_b[:cs_n] + new_compressed + pdf_b[ce_n:]

    stream_delta = len(new_compressed) - (ce - cs)
    total_shift_after_ce = length_delta + stream_delta

    sx_pos = pdf_b.rfind(b"startxref")
    if sx_pos < 0:
        return None
    m_sx = re.search(rb"startxref\s+(\d+)", pdf_b[sx_pos:])
    if not m_sx:
        return None
    orig_xref_off = int(m_sx.group(1))
    new_xref_off = orig_xref_off + total_shift_after_ce

    body = pdf_b[new_xref_off:]
    m_h = re.match(rb"xref\s*\n\s*(\d+)\s+(\d+)\s*\n", body)
    if not m_h:
        return None
    first = int(m_h.group(1))
    count = int(m_h.group(2))
    body_start = m_h.end()

    new_lines = [b"xref\n", f"{first} {count}\n".encode()]
    for i in range(count):
        n = first + i
        line = body[body_start + i * 20: body_start + i * 20 + 20]
        if n == 0 or line[17:18] != b"n":
            new_lines.append(bytes(line))
            continue
        old_off = int(line[:10])
        if old_off >= ce:
            new_off = old_off + total_shift_after_ce
        else:
            new_off = old_off
        new_lines.append(f"{new_off:010d} 00000 n \n".encode())
    new_xref_block = b"".join(new_lines)

    trailer_pos = pdf_b.find(b"trailer", new_xref_off)
    if trailer_pos < 0:
        return None
    pdf_b = pdf_b[:new_xref_off] + new_xref_block + pdf_b[trailer_pos:]

    sx_pos2 = pdf_b.rfind(b"startxref")
    eof_pos = pdf_b.find(b"%%EOF", sx_pos2)
    if eof_pos < 0:
        return None
    pdf_b = (
        pdf_b[:sx_pos2]
        + f"startxref\n{new_xref_off}\n%%EOF\n".encode()
    )
    return pdf_b


_ORIG_DATE_CARD     = "17.02.2026  00:57:35"
_ORIG_AMOUNT_CARD     = "10 000 "
_ORIG_AMOUNT_NET_CARD = "34 778,33 "
_ORIG_SENDER_CARD   = "Ирина Ларицкая"
_ORIG_RECEIVER_CARD = "Эрик А."
_ORIG_BANK_CARD     = "Сбербанк"
_ORIG_CARD_NUM      = "220220******3269"
_ORIG_RECEIPT_CARD  = "Квитанция  \u2116 1-129-063-421-094"
ORIG_TEMPLATE_CARD       = os.path.join(_DIR, "templates", "T_original.pdf")
ORIG_TEMPLATE_CARD_BANK  = os.path.join(_DIR, "templates", "T_card_sber_original.pdf")
_CARD_SBER_SEED          = os.path.join(
    os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "т банк", "по карте на сбербанк2.pdf")


def _ensure_card_sber_bank_template() -> None:
    """Банковский card_sber shell (Keywords | DOCS-2035), не T_original (DOCS-349)."""
    import shutil
    if os.path.isfile(ORIG_TEMPLATE_CARD_BANK):
        return
    if os.path.isfile(_CARD_SBER_SEED):
        os.makedirs(os.path.dirname(ORIG_TEMPLATE_CARD_BANK), exist_ok=True)
        shutil.copy2(_CARD_SBER_SEED, ORIG_TEMPLATE_CARD_BANK)
        logger.info("card_sber shell: %s", ORIG_TEMPLATE_CARD_BANK)


def _card_generation_base_path(prepared: Optional[Dict] = None) -> str:
    _ensure_card_sber_bank_template()
    if os.path.isfile(ORIG_TEMPLATE_CARD_BANK):
        return ORIG_TEMPLATE_CARD_BANK
    return ORIG_TEMPLATE_CARD


def _card_reg_texts(prepared: Dict, *, include_receipt: bool = True) -> list:
    p = prepared
    texts = [
        p["new_date"], p["sender"], p["receiver"], p["bank"], p["card"],
        p["new_amount"], p["new_commission"],
        f"Квитанция  \u2116 {p['receipt_raw']}",
    ]
    return texts

# Известные BIN-ы Т-Банка — для автоформатирования номера карты
_TBANK_BINS = ["220220", "521737", "437772", "553691", "521324"]
_DEFAULT_BIN = "220220"


def _format_card_num(raw: str) -> str:
    """Приводит номер карты к формату BIN******XXXX.

    Правила:
    - Если уже похоже на XXXXXX*****XXXX (16 симв.) → оставляем BIN как вводит пользователь.
    - Если только 4 цифры или «*XXXX» / «****XXXX» → достраиваем до «220220******XXXX».
    - Если 4–6 цифр в конце и нет явного BIN → «220220******XXXX».
    """
    raw = raw.strip()
    # Уже полный формат (16 символов вида DDDDDD***...DDD) — BIN не переписываем.
    if len(raw) == 16 and re.match(r"^\d{6}\*+\d{4}$", raw):
        return raw
    # Формат **** **** **** XXXX → берём последние 4 цифры
    m_spaced = re.match(r"^\*{4}\s+\*{4}\s+\*{4}\s+(\d{4})$", raw)
    if m_spaced:
        return f"{_DEFAULT_BIN}******{m_spaced.group(1)}"
    # Хвост из 4 цифр (с любым количеством * впереди)
    m_tail = re.match(r"^[\*\s]*(\d{4})$", raw)
    if m_tail:
        return f"{_DEFAULT_BIN}******{m_tail.group(1)}"
    # Строка из 4–6 цифр (без маски) → тоже дополняем
    m_digits = re.match(r"^(\d{4,6})$", raw)
    if m_digits:
        last4 = m_digits.group(1)[-4:]
        return f"{_DEFAULT_BIN}******{last4}"
    # Иначе — оставляем как есть
    return raw


def _normalize_card_commission(raw: str) -> Tuple[str, bool]:
    """(текст комиссии '0 ' / '221,67 ', есть ли ненулевая комиссия для split сумм)."""
    from tbank_sbp_stealth import _DISPLAY_COMMISSION
    from tbank_dynamic import parse_money_parts

    raw = str(raw or "").strip()
    low = raw.lower().replace("₽", "").strip()
    if not raw or low in ("", "нет", "без", "без комиссии", "авто", "-", "0"):
        return _DISPLAY_COMMISSION, False
    rub, kop = parse_money_parts(raw)
    if rub == 0 and kop == 0:
        return _DISPLAY_COMMISSION, False
    if kop:
        return f"{rub},{kop:02d} ", True
    return f"{rub:,}".replace(",", " ") + " ", True


def _gen_receipt_num_safe(
    forbid: str = "8",
    kind: str = "card_sber",
    op_date: Optional[str] = None,
) -> str:
    """Номер квитанции: реальный скелет из корпуса, новый хвост (3 цифры)."""
    from tbank_corpus import gen_receipt_number
    return gen_receipt_number(kind, op_date=op_date, forbid=forbid)


def _strip_yo_tverd(text: str) -> str:
    """Compatibility hook: preserve the accepted payload exactly."""
    return text


def _prepare_card_data(data: Dict) -> Dict:
    new_date = _fmt_date(str(data.get("date_time", _ORIG_DATE_CARD)))

    amount_raw = str(data.get("amount", "10000"))
    from tbank_dynamic import (
        format_amount_like_template,
        format_decimal_amount_like_template,
        parse_money_parts,
    )
    from tbank_sbp_stealth import _normalize_tbank_sender, _normalize_tbank_receiver

    gross_fmt = format_amount_like_template(amount_raw, _ORIG_AMOUNT_CARD)

    new_commission, has_nonzero_commission = _normalize_card_commission(
        str(data.get("commission", "")))

    total_raw = str(data.get("amount_total", "")).strip()
    if has_nonzero_commission:
        # Банк: Итого (крупно) = сумма перевода; строка «Сумма» = за вычетом комиссии.
        if total_raw:
            net_fmt = format_decimal_amount_like_template(
                total_raw, _ORIG_AMOUNT_NET_CARD)
        else:
            gross_rub, _ = parse_money_parts(amount_raw)
            comm_rub, comm_kop = parse_money_parts(str(data.get("commission", "")))
            net_kop = gross_rub * 100 - (comm_rub * 100 + comm_kop)
            if net_kop < 0:
                net_kop = 0
            net_rub, net_rem = divmod(net_kop, 100)
            net_fmt = format_decimal_amount_like_template(
                f"{net_rub},{net_rem:02d}", _ORIG_AMOUNT_NET_CARD)
        new_amount_total = gross_fmt
        new_amount = net_fmt
    elif total_raw:
        new_amount = format_amount_like_template(amount_raw, _ORIG_AMOUNT_CARD)
        new_amount_total = format_amount_like_template(total_raw, _ORIG_AMOUNT_CARD)
    else:
        new_amount = gross_fmt
        new_amount_total = gross_fmt

    receipt_raw = str(data.get("receipt_num", "авто")).strip()
    receipt_auto = receipt_raw.lower() in ("авто", "auto", "-", "")
    if receipt_auto:
        from tbank_corpus import gen_receipt_number
        receipt_raw = gen_receipt_number("card_sber", op_date=new_date)
        logger.info(f"auto receipt: {receipt_raw}")
    else:
        from tbank_corpus import normalize_receipt_num
        receipt_raw = normalize_receipt_num(receipt_raw)

    sender_raw = str(data.get("sender", _ORIG_SENDER_CARD))
    sender = _strip_yo_tverd(_normalize_tbank_sender(sender_raw) or sender_raw)
    receiver = _strip_yo_tverd(
        _normalize_tbank_receiver(str(data.get("receiver", _ORIG_RECEIVER_CARD)))
    )
    bank = _strip_yo_tverd(str(data.get("recipient_bank", _ORIG_BANK_CARD)))

    return {
        "new_date":         new_date,
        "new_amount":       new_amount,
        "new_amount_total": new_amount_total,
        "sender":           sender,
        "receiver":         receiver,
        "bank":             bank,
        "card":             _format_card_num(str(data.get("card", _ORIG_CARD_NUM))),
        "new_commission":   new_commission,
        "has_nonzero_commission": has_nonzero_commission,
        "receipt_raw":      receipt_raw,
        "_receipt_auto":    receipt_auto,
        "_user_sender": sender,
        "_user_receiver": receiver,
        "_user_bank": bank,
        "_shell_sender": _ORIG_SENDER_CARD,
        "_shell_receiver": _ORIG_RECEIVER_CARD,
        "_shell_bank": _ORIG_BANK_CARD,
    }


def _extract_card_fields(path: str) -> Optional[Dict[str, str]]:
    """Текстовые значения из card_sber чека (По номеру карты)."""
    import fitz

    try:
        doc = fitz.open(path)
        lines = [l.strip() for l in doc[0].get_text().split("\n") if l.strip()]
        doc.close()
    except Exception:
        return None
    if "По номеру карты" not in lines:
        return None

    def before(label: str) -> str:
        for i, l in enumerate(lines):
            if l == label and i > 0:
                return lines[i - 1]
        return ""

    def after(label: str) -> str:
        for i, l in enumerate(lines):
            if l == label and i + 1 < len(lines):
                return lines[i + 1]
        return ""

    receipt = next((l for l in lines if l.startswith("Квитанция")), "")
    amount_big = ""
    for i, l in enumerate(lines):
        if l == "Итого" and i + 1 < len(lines):
            amount_big = lines[i + 1].replace("i", "").strip() + " "
            break
    amount_small = before("Сумма")
    if amount_small.endswith("i"):
        amount_small = amount_small[:-1].strip() + " "
    comm = after("Комиссия")
    if comm.endswith("i"):
        comm = comm[:-1].strip() + " "
    elif comm and not comm.endswith(" "):
        comm = comm + " "
    if not comm.strip():
        comm = "Без комиссии"
    return {
        "date": lines[0],
        "amount": amount_small if amount_small else amount_big,
        "amount_total": amount_big or amount_small,
        "sender": before("Отправитель"),
        "card": after("Карта получателя"),
        "receiver": after("Получатель"),
        "bank": after("Банк получателя"),
        "commission": comm,
        "receipt": receipt,
    }


def _default_card_orig() -> Dict[str, str]:
    orig = _extract_card_fields(_card_generation_base_path())
    if orig:
        return orig
    return {
        "date": _ORIG_DATE_CARD,
        "amount": _ORIG_AMOUNT_CARD,
        "amount_total": _ORIG_AMOUNT_CARD,
        "sender": _ORIG_SENDER_CARD,
        "receiver": _ORIG_RECEIVER_CARD,
        "bank": _ORIG_BANK_CARD,
        "card": _ORIG_CARD_NUM,
        "commission": "0 ",
        "receipt": _ORIG_RECEIPT_CARD,
    }


def _find_best_card_donor(prepared: Dict) -> Optional[Tuple[str, Dict, List[str], List[str]]]:
    from tbank_corpus import (
        corpus_paths, pick_donor, pick_h471_card_sber_exact_donor,
    )
    from tbank_donor_fit import adapt_card_prepared, pick_tbank_donor, amount_slot_fits
    from tbank_sbp_stealth import _amount_slot_ok

    reg_texts = _card_reg_texts(prepared, include_receipt=False)
    paths = list(corpus_paths("card_sber") or [])
    face = " ".join(
        str(prepared.get(k) or "")
        for k in (
            "new_sender", "new_receiver", "new_bank",
            "new_card", "new_amount", "new_date",
        )
    )
    painted_n = max(59, min(63, len({c for c in face if c.isalnum() or ord(c) > 127})))
    exact_pref = pick_h471_card_sber_exact_donor(
        painted_n=painted_n, op_date=prepared.get("new_date"),
    )
    if exact_pref and exact_pref in paths:
        paths = [exact_pref] + [p for p in paths if p != exact_pref]
    preferred = pick_donor("card_sber", prepared.get("new_date"))
    if preferred and preferred in paths and preferred != exact_pref:
        paths = [preferred] + [p for p in paths if p != preferred]

    def _score(ctx, orig, adapted):
        score = 40
        if orig.get("date", "").strip() == adapted["new_date"].strip():
            score += 60
        comm = (orig.get("commission") or "").strip()
        if not adapted.get("has_nonzero_commission"):
            if comm.startswith("0"):
                score += 25
            elif "Без" in comm:
                score -= 5
        return score

    return pick_tbank_donor(
        paths,
        prepared,
        adapt_fn=adapt_card_prepared,
        reg_texts=reg_texts + [f"Квитанция  \u2116 {prepared['receipt_raw']}"],
        med_texts=[prepared["new_amount_total"], prepared["new_amount"]],
        extract_orig=_extract_card_fields,
        score_fn=_score,
        min_score=20,
        amount_slot_ok=lambda ctx, oa, na, med: amount_slot_fits(oa, na),
        amount_total_key="new_amount_total",
        allow_font_extend=True,
        max_miss_r=0,
    )


def _try_donor_orig_card(prepared: Dict) -> Optional[bytes]:
    """In-place правка банковского card_sber PDF, metadata донора сохраняем."""
    hit = _find_best_card_donor(prepared)
    if not hit:
        return None
    donor, prepared, miss_r, miss_m = hit
    if miss_r or miss_m:
        return None
    orig = _extract_card_fields(donor)
    if not orig:
        return None
    logger.info("🔵 card donor-orig: %s", os.path.basename(donor))
    from tbank_channel_common import prepare_donor_for_orig
    from tbank_corpus import remix_receipt_d_only
    from tbank_donor_fit import adapt_card_prepared

    reg_texts = _card_reg_texts(prepared)
    hit2 = prepare_donor_for_orig(
        donor,
        prepared,
        adapt_fn=adapt_card_prepared,
        reg_texts=reg_texts,
        med_texts=[prepared["new_amount_total"], prepared["new_amount"]],
        allow_font_extend=True,
    )
    if not hit2:
        return None
    pdf_path, prepared, skip_size_pad = hit2
    orig = _extract_card_fields(pdf_path) or orig
    # Date/amount digit entropy often overshoots donor /Length — remix
    # receipt (auto only) until OpenPDF flate lands exactly (phone path).
    # Prefer donor receipt first (same prefix/CID entropy as flate-stable
    # corpus); gen_receipt's forced 1-132- often never hits /Length.
    from tbank_corpus import normalize_receipt_num

    donor_rcp = ""
    if orig.get("receipt"):
        m = re.search(r"(\d-\d{3}-\d{3}-\d{3}-\d{3})", orig["receipt"])
        if m:
            donor_rcp = normalize_receipt_num(m.group(1))
    auto_rcp = bool(prepared.get("_receipt_auto"))
    base_num = donor_rcp or str(prepared.get("receipt_raw") or "")
    if auto_rcp and donor_rcp:
        prepared["receipt_raw"] = donor_rcp
    max_rcp_try = 64 if auto_rcp else 1

    def _remix_keep_prefix(base: str) -> str:
        parts = str(base or "").split("-")
        if len(parts) == 5 and all(parts):
            import random as _rnd
            return (
                f"{parts[0]}-{parts[1]}-"
                f"{_rnd.randint(0, 999):03d}-"
                f"{_rnd.randint(0, 999):03d}-"
                f"{_rnd.randint(0, 999):03d}"
            )
        return remix_receipt_d_only(base)

    for rcp_i in range(max_rcp_try):
        if auto_rcp and rcp_i > 0:
            prepared["receipt_raw"] = _remix_keep_prefix(base_num)
        pdf = _try_orig_mode_card_on(
            pdf_path, orig, prepared, preserve_metadata=True, preserve_donor=True,
            skip_size_pad=skip_size_pad,
        )
        if pdf is not None:
            if rcp_i > 0:
                logger.info(
                    "  [DONOR-ORIG] card exact flate via receipt remix try %d",
                    rcp_i + 1,
                )
            return pdf
    return None


def _pick_card_dynamic_base(prepared: Dict) -> Tuple[str, Dict[str, str]]:
    """Донор card_sber из корпуса — ближе к банковскому F1/F2 кластеру, чем T_original."""
    from tbank_orig_mode import OrigContext
    from tbank_corpus import corpus_paths

    default_orig = _default_card_orig()
    best_path = ""
    best_orig = default_orig
    best_score = 10 ** 9

    p = prepared
    reg_texts = [
        p["new_date"], p["sender"], p["receiver"], p["bank"], p["card"], p["new_amount"],
        p["new_commission"],
        f"Квитанция  \u2116 {p['receipt_raw']}",
    ]

    candidates = corpus_paths("card_sber") or []
    if not candidates:
        return ORIG_TEMPLATE_CARD, default_orig

    for path in candidates:
        orig = _extract_card_fields(path)
        if orig is None:
            continue
        ctx = OrigContext()
        if not ctx.load(path):
            continue
        ok_r, miss_r = ctx.can_render_reg(*reg_texts)
        ok_m, miss_m = ctx.can_render_med(p["new_amount_total"], p["new_amount"])
        score = len(miss_r) * 10 + len(miss_m) * 5
        comm = (orig.get("commission") or "").strip()
        if comm.startswith("0"):
            score -= 2
        if not p.get("has_nonzero_commission"):
            if comm.startswith("0"):
                score -= 5
            elif "Без" in comm:
                score += 4
        if score < best_score:
            best_score = score
            best_path = path
            best_orig = orig

    if best_path != ORIG_TEMPLATE_CARD and best_path:
        logger.info(
            "🟢 card base [%s] miss score=%d",
            os.path.basename(best_path), best_score,
        )
    return best_path or ORIG_TEMPLATE_CARD, best_orig


def _try_orig_mode_card_on(
    template_path: str,
    orig: Dict[str, str],
    prepared: Dict,
    preserve_metadata: bool = False,
    preserve_donor: bool = False,
    skip_size_pad: bool = False,
) -> Optional[bytes]:
    from tbank_orig_mode import OrigContext
    from tbank_sbp_stealth import _prune_donor_font_subset, _fix_stream_separators, _pad_pdf_to_exact_size

    ctx = OrigContext()
    if not ctx.load(template_path):
        return None

    p = prepared
    reg_texts = [p["new_date"], p["sender"], p["receiver"], p["bank"], p["card"],
                 p["new_amount"], p["new_commission"],
                 f"Квитанция  \u2116 {p['receipt_raw']}"]

    ok_r, miss_r = ctx.can_render_reg(*reg_texts)
    ok_m, miss_m = ctx.can_render_med(p["new_amount_total"], p["new_amount"])
    if not (ok_r and ok_m):
        logger.info(f"🟡 orig [{os.path.basename(template_path)}] skip: reg={miss_r} med={miss_m}")
        return None

    pdf = bytearray(ctx.pdf_bytes)
    donor_size = len(ctx.pdf_bytes)
    cs, ce, raw = ctx.cs_cs, ctx.cs_ce, ctx.cs_raw
    stream = bytes(ctx.cs_dec)
    orig_comp_len = len(raw)

    def enc_r(t): return ctx.enc(t, medium=False)
    def enc_m(t): return ctx.enc(t, medium=True)

    from tbank_orig_mode import replace_field_preserve_tm, replace_tj_bytes_inplace, fit_text_to_enc_len, replace_amount_preserve_tm

    def replace_tj(stream, old_b, new_b, old_t, new_t, sz, medium):
        enc_fn = enc_m if medium else enc_r
        if not old_b:
            old_b = enc_fn(old_t)
        if not new_b:
            new_b = enc_fn(new_t)
        if not old_b or not new_b:
            return stream, False

        needle = b"(" + old_b + b")Tj"
        pos = stream.find(needle)
        if pos < 0:
            return stream, False
        if old_b == new_b:
            return stream, True
        look_from = max(0, pos - 200)
        region = stream[look_from:pos]
        tms = list(_TM_RE.finditer(region))
        if not tms:
            return stream[:pos] + b"(" + new_b + b")Tj" + stream[pos + len(needle):], True
        last = tms[-1]
        old_x = float(last.group(1))
        old_y = last.group(2).decode()
        old_w = ctx.text_width(old_t, sz, medium=medium)
        new_w = ctx.text_width(new_t, sz, medium=medium)
        new_x = old_x + (old_w - new_w)
        if abs(new_x - old_x) < 0.0001:
            return stream[:pos] + b"(" + new_b + b")Tj" + stream[pos + len(needle):], True
        new_tm = f"1 0 0 1 {_fmt_coord(new_x)} {old_y} Tm".encode("ascii")
        return (
            stream[:look_from + last.start()] + new_tm
            + stream[look_from + last.end():pos] + b"(" + new_b + b")Tj"
            + stream[pos + len(needle):]
        ), True

    o_amt = orig.get("amount") or _ORIG_AMOUNT_CARD
    o_total = orig.get("amount_total") or o_amt
    o = orig

    ok = {}
    if p["new_date"] == o.get("date"):
        ok["date"] = True
    else:
        if preserve_donor or preserve_metadata:
            stream, _, ok["date"] = replace_field_preserve_tm(
                ctx, stream, o["date"], p["new_date"], medium=False,
            )
        else:
            stream, ok["date"] = _replace_once(stream, enc_r(o["date"]), enc_r(p["new_date"]))
    if p["new_amount"].strip() == o_amt.strip():
        ok["amt_big"] = ok["amt_small"] = True
    else:
        if preserve_donor or preserve_metadata:
            stream, _, ok["amt_big"] = replace_amount_preserve_tm(
                ctx, stream, o_total, p["new_amount_total"], medium=True)
            stream, _, ok["amt_small"] = replace_amount_preserve_tm(
                ctx, stream, o_amt, p["new_amount"], medium=False)
        else:
            stream, ok["amt_big"] = replace_tj(
                stream, enc_m(o_total), enc_m(p["new_amount_total"]),
                o_total, p["new_amount_total"], 16.0, True)
            stream, ok["amt_small"] = replace_tj(
                stream, enc_r(o_amt), enc_r(p["new_amount"]),
                o_amt, p["new_amount"], 9.0, False)
    for name, key in [
        ("sender", "sender"),
        ("receiver", "receiver"),
        ("bank", "bank"),
        ("card", "card"),
    ]:
        old = o.get(key, "")
        new = p[key]
        if new == old:
            ok[name] = True
        elif preserve_donor or preserve_metadata:
            # Never trim FIO/bank/card to donor CID length — fall back to Tm.
            stream, fitted, ok[name] = replace_field_preserve_tm(
                ctx, stream, old, new, medium=False, allow_trim=False)
            if ok[name] and (fitted or "").rstrip() != (new or "").rstrip():
                ok[name] = False
            if not ok[name]:
                stream, ok[name] = replace_tj(
                    stream, enc_r(old), enc_r(new), old, new, 9.0, False)
        else:
            stream, ok[name] = replace_tj(stream, enc_r(old), enc_r(new), old, new, 9.0, False)

    o_comm = orig.get("commission") or "Без комиссии"
    if (
        not p.get("has_nonzero_commission")
        and p["new_commission"].strip() in ("0", "0 ")
        and "Без" in o_comm
    ):
        ok["commission"] = True
    else:
        stream, ok["commission"] = replace_tj(
            stream, enc_r(o_comm), enc_r(p["new_commission"]),
            o_comm, p["new_commission"], 9.0, False)
        if ok["commission"] and not (preserve_donor or preserve_metadata):
            from tbank_sbp_stealth import _inject_f3_ruble_after_tj
            stream = _inject_f3_ruble_after_tj(stream, enc_r(p["new_commission"]))

    o_rcp = orig.get("receipt") or _ORIG_RECEIPT_CARD
    from tbank_corpus import normalize_receipt_num
    receipt_num = normalize_receipt_num(p["receipt_raw"])
    nr = f"Квитанция  \u2116 {receipt_num}"
    stream, ok["receipt"] = _replace_once(stream, enc_r(o_rcp), enc_r(nr))

    for k, v in ok.items():
        logger.info(f"  {'OK' if v else 'FAIL'} {k}")
    if not all(ok.values()):
        return None

    if not (preserve_donor or preserve_metadata):
        from tbank_sbp_stealth import _normalize_content_stream_footer
        stream = _normalize_content_stream_footer(stream)

    if stream == ctx.cs_dec:
        pdf[cs:ce] = raw
    else:
        from tbank_channel_common import compress_orig_stream

        new_compressed = compress_orig_stream(stream, orig_comp_len)
        if new_compressed is None:
            return None
        if len(new_compressed) != orig_comp_len:
            rebuilt = _patch_length_and_rebuild(pdf, cs, ce, new_compressed)
            if rebuilt is None:
                return None
            pdf = bytearray(rebuilt)
        else:
            pdf[cs:ce] = new_compressed

    if preserve_donor or preserve_metadata:
        result = bytes(pdf)
        prune = False
    else:
        result = bytes(pdf)
        prune = True
    from tbank_channel_common import finalize_orig_result

    result = finalize_orig_result(
        result,
        new_date=p["new_date"],
        preserve_donor=(preserve_metadata or preserve_donor),
        skip_size_pad=skip_size_pad,
        donor_size=donor_size,
        ctx=ctx,
        stream=stream,
        prune=prune,
    )
    tag = "DONOR-ORIG" if preserve_metadata else "ORIG"
    logger.info(f"🟡 {tag} [{os.path.basename(template_path)}]: {len(result)} bytes")
    return result


def _try_orig_mode_card(prepared: Dict) -> Optional[bytes]:
    try:
        from tbank_corpus import (
            pick_donor, pick_h471_card_sber_exact_donor, template_paths,
        )
        paths = template_paths("card_sber", ORIG_TEMPLATE_CARD)
        face = " ".join(
            str(prepared.get(k) or "")
            for k in (
                "new_sender", "new_receiver", "new_bank",
                "new_card", "new_amount", "new_date",
            )
        )
        painted_n = max(
            59,
            min(63, len({c for c in face if c.isalnum() or ord(c) > 127})),
        )
        exact_pref = pick_h471_card_sber_exact_donor(
            painted_n=painted_n, op_date=prepared.get("new_date"),
        )
        if exact_pref and exact_pref in paths:
            paths = [exact_pref] + [p for p in paths if p != exact_pref]
        preferred = pick_donor("card_sber", prepared.get("new_date"))
        if preferred and preferred in paths and preferred != exact_pref:
            paths = [preferred] + [p for p in paths if p != preferred]
    except Exception:
        paths = [ORIG_TEMPLATE_CARD]

    default_orig = _default_card_orig()

    for path in paths:
        orig = _extract_card_fields(path) if path != ORIG_TEMPLATE_CARD else None
        if orig is None and path == ORIG_TEMPLATE_CARD:
            orig = default_orig
        elif orig is None:
            continue
        res = _try_orig_mode_card_on(path, orig, prepared)
        if res is not None:
            return res
    return None


def _build_dynamic_card(prepared: Dict) -> Optional[bytes]:
    """Card_sber dynamic: same soft/PUA mosaic-safe path as card_tbank / nocomm."""
    from tbank_dynamic import build_dynamic_tbank
    from tbank_channel_common import size_matches_original
    from tbank_sbp_stealth import (
        _inject_f3_ruble_after_tj,
        _F1_CARD_DEC_LO,
        _F1_CARD_DEC_HI,
        _F1_CARD_DEC_TARGET,
    )
    from tbank_corpus import normalize_receipt_num

    p = prepared
    base_path = _card_generation_base_path(p)
    orig_f = _extract_card_fields(base_path) or _default_card_orig()
    if not os.path.isfile(base_path):
        logger.error("Card template missing: %s", base_path)
        return None

    o_date = orig_f["date"]
    o_amt = orig_f["amount"]
    o_amt_total = orig_f["amount_total"]
    o_sender = orig_f["sender"]
    o_receiver = orig_f["receiver"]
    o_bank = orig_f["bank"]
    o_card = orig_f["card"]
    o_receipt = orig_f["receipt"]
    o_comm = orig_f.get("commission") or "0 "
    need_r = _card_reg_texts(p)

    def apply(stream, er, em, rtj, replace_once, _er_soft=None):
        ok: Dict[str, bool] = {}
        stream, ok["date"] = replace_once(stream, o_date, p["new_date"])
        stream, ok["amt_small"] = rtj(
            stream, o_amt, p["new_amount"], 9.0, False, occurrence=2)
        if not ok["amt_small"]:
            stream, ok["amt_small"] = rtj(
                stream, o_amt, p["new_amount"], 9.0, False, occurrence=1)
        stream, ok["amt_big"] = rtj(
            stream, o_amt_total, p["new_amount_total"], 16.0, True, occurrence=1)
        # Soft may remap commission digits inside rtj — inject against stream needle
        # by encoding the same softed value (re-soft via rtj path: use er on soft result).
        comm = p["new_commission"]
        stream, ok["commission"] = rtj(stream, o_comm, comm, 9.0, False)
        if ok["commission"]:
            # Prefer matching whatever rtj wrote: try softed candidates from digit pool.
            from tbank_sbp_stealth import _SOFT_DIGIT_POOL
            injected = False
            for trial in (comm,):
                stream2 = _inject_f3_ruble_after_tj(stream, er(trial))
                if stream2 is not stream:
                    stream = stream2
                    injected = True
                    break
            if not injected:
                # Last resort: scan — commission TJ already in stream; inject by raw er(comm)
                stream = _inject_f3_ruble_after_tj(stream, er(comm))
        for name, old, new in [
            ("sender", o_sender, p["sender"]),
            ("receiver", o_receiver, p["receiver"]),
            ("bank", o_bank, p["bank"]),
            ("card", o_card, p["card"]),
        ]:
            stream, ok[name] = rtj(stream, old, new, 9.0, False)
        receipt_num = normalize_receipt_num(p["receipt_raw"])
        nr = f"Квитанция  \u2116 {receipt_num}"
        stream, ok["receipt"] = replace_once(stream, o_receipt, nr)
        return stream, ok

    return build_dynamic_tbank(
        orig_path=base_path,
        need_r_texts=need_r,
        need_m_texts=[p["new_amount_total"], p["new_amount"]],
        apply_replacements=apply,
        metadata_date=p["new_date"],
        patch_metadata_fn=_patch_pdf_metadata,
        log_label="🟡 CARD_SBER DYNAMIC",
        f1_dec_band=(_F1_CARD_DEC_LO, _F1_CARD_DEC_HI),
        f1_dec_target=_F1_CARD_DEC_TARGET,
        f1_blank_on_trim=True,
        post_process=lambda pdf: (
            pdf
            if size_matches_original(pdf, base_path, drift=2048)
            else (logger.warning("CARD_SBER size drift %d — ship", len(pdf)) or pdf)
        ),
    )


def create_tbank_stealth(data: Dict) -> Optional[bytes]:
    """Card_sber: donor-orig first, Jasper fallback for live."""
    from tbank_dynamic import create_tbank_pipeline
    from tbank_channel_common import pdf_face_matches_user

    prepared = _prepare_card_data(data)
    pdf = create_tbank_pipeline(
        prepared,
        [_try_donor_orig_card, _build_dynamic_card],
        channel="card_sber",
        dynamic_builder=_build_dynamic_card,
    )
    if pdf is None:
        try:
            pdf = _build_dynamic_card(dict(prepared))
        except Exception as exc:
            logger.error("CARD_SBER LAW1 dynamic retry: %s", exc)
            pdf = None
    if pdf is None:
        return None
    ok, why = pdf_face_matches_user(
        pdf,
        prepared,
        donor_senders=(_ORIG_SENDER_CARD,),
        donor_banks=(_ORIG_BANK_CARD,),
    )
    if not ok:
        logger.error("CARD_SBER: %s — finish anyway", why)
    from tbank_dynamic import _tbank_finish_non_sbp_ship
    return _tbank_finish_non_sbp_ship(
        pdf, height=471, prepared=prepared, channel="card_sber",
    )


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    sample = {
        "date_time": "07.05.2026, 12:34",
        "amount": "75000",
        "sender": "Иван Иванович Иванов",
        "receiver": "Пётр Петров П.",
        "recipient_bank": "ВТБ",
        "card": "220220******1234",
    }
    res = create_tbank_stealth(sample)
    if res:
        out = os.path.join(_DIR, "test_tbank_stealth.pdf")
        with open(out, "wb") as f:
            f.write(res)
        print(f"saved: {out} ({len(res)} bytes)")
    else:
        print("FAIL")
