"""
OTP CARD STEALTH — генератор чеков «На карту другого банка» ОТП Банка.

Базовый шаблон: `templates/OTP_card_original.pdf` (mPDF 8.3.1).

Стратегия — аналогична `otp_sbp_stealth`:
  1. orig-mode: если все символы пользовательских данных лежат в orig
     subset шрифтов (F2/F3/F4) — меняем нужные `[ … ] TJ` блоки в content
     stream IN-PLACE через _pad_to_compressed_size(). Размер итогового PDF
     ОСТАЁТСЯ РАВНЫМ оригиналу (56 443 B).

  2. unlocked-mode (TODO): если каких-то символов не хватает — пересобрать
     шаблон с расширенным subset через otp_card_unlock_template (пока не
     реализовано — fallback на orig-mode с предупреждением).

В content stream:
  • строки кодируются как Identity-H (`(` … `)` ⇒ utf-16be байты),
  • CID == GID == Unicode codepoint (mPDF-фишка),
  • `[ (text) -N ( ) (text) … ] TJ` — массив с adjustments между «словами»
    (mPDF разбивает на группы для эмуляции justify/spacing).

Поля чека:
  • header           — «Квитанция №NNNNNNNN»            (F3 20pt, left)
  • date_time        — «DD.MM.YYYY HH:MM:SS (МСК)»       (F2 12pt grey, left)
  • sender           — ФИО плательщика                   (F2 12pt, right)
  • passport         — маска паспорта                    (F2 12pt, right)
  • card             — «XXXXXXXXXXXXXXXX» (16 цифр)      (F2 12pt, right)
  • amount_no_comm   — «N XXX,XX ₽»                      (F2 12pt, right)
  • commission       — «XXX,XX ₽»                        (F2 12pt, right)
  • amount_total     — «N XXX,XX ₽»                      (F3 24pt, right, bold)

Все остальные поля (лейблы, legal-text, синяя рамка АО ОТП Банк) остаются
байт-в-байт как в шаблоне.
"""
from __future__ import annotations

import os
import re
import zlib
import logging
from typing import Dict, List, Optional, Tuple

import fitz

from otp_card_orig_mode import OtpCardOrigContext

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
ORIG_TEMPLATE = os.path.join(_DIR, "templates", "OTP_card_original.pdf")

_TARGET_PDF_SIZE = 56_443  # точный размер `templates/OTP_card_original.pdf` (orig mPDF; статус «ИСПОЛНЕНО» без смены веса файла)


# ─── Эталонные значения (из шаблона) ────────────────────────────────────────
_ORIG_RECEIPT_NUM   = "99161372"
_ORIG_DATE_TIME     = "03.05.2026 16:33:49"
_ORIG_SENDER        = "АНТОН ИГОРЕВИЧ Ш******"
_ORIG_PASSPORT      = "5*** *****1"            # как в шаблоне после декодирования TJ
_ORIG_CARD          = "2204310306568331"
_ORIG_AMOUNT_NC     = " 2  520,00  \u20bd"     # « 2 520,00 ₽» с двойными пробелами
_ORIG_COMM          = " 150,00  \u20bd"
_ORIG_TOTAL         = " 2  670,00  \u20bd"

_ORIG_HEADER        = f"Квитанция \u2116{_ORIG_RECEIPT_NUM}"   # «Квитанция №99161372»
_ORIG_DATE_FULL     = f"{_ORIG_DATE_TIME} (МСК)"

# Правый край right-aligned колонки (рассчитан из orig): _RIGHT_X.
# Можно посчитать точно для каждой строки, но они все выровнены по одному краю
# (примерно 559.198 — край рамки + paddings). Возьмём из эталона.
_RIGHT_X = 559.198
_TC = 0.113      # character spacing


# ─── Низкоуровневые PDF-утилиты ─────────────────────────────────────────────

def _find_obj_pos(pdf: bytes, xref: int) -> Tuple[int, int]:
    pos = pdf.find(f"\n{xref} 0 obj".encode())
    if pos < 0:
        pos = pdf.find(f"{xref} 0 obj".encode())
    if pos < 0:
        raise ValueError(f"obj {xref} not found")
    end = pdf.find(b"endobj", pos) + len(b"endobj")
    return pos, end


def _replace_stream_raw(pdf: bytes, xref: int, new_stream: bytes) -> bytes:
    pos, end = _find_obj_pos(pdf, xref)
    block = pdf[pos:end]
    m = re.search(rb"<<(.*?)>>", block, re.S)
    if not m:
        raise ValueError("dict not found")
    dict_body = m.group(1)
    dict_body = re.sub(
        rb"/Length\s+\d+", f"/Length {len(new_stream)}".encode(), dict_body, count=1
    )
    leading = block[:m.start()]
    new_block = (
        leading + b"<<" + dict_body + b">>\nstream\n" + new_stream + b"\nendstream\nendobj"
    )
    return pdf[:pos] + new_block + pdf[end:]


def _pad_to_compressed_size(stream: bytes, target_size: int) -> Optional[bytes]:
    """Подбирает padding и compress level так, чтобы compressed size == target."""
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
    p: Dict[str, str] = {}

    receipt_num = (data.get("receipt_num") or _ORIG_RECEIPT_NUM).strip()
    if not receipt_num.isdigit():
        receipt_num = _ORIG_RECEIPT_NUM
    p["receipt_num"] = receipt_num
    p["header"]      = f"Квитанция \u2116{receipt_num}"

    date_time = (data.get("date_time") or _ORIG_DATE_TIME).strip()
    if " " not in date_time and "T" in date_time:
        date_time = date_time.replace("T", " ")
    p["date_time"] = date_time
    p["date_full"] = f"{date_time} (МСК)"

    p["sender"]   = (data.get("sender")   or _ORIG_SENDER).strip()
    p["passport"] = (data.get("passport") or data.get("passport_data") or _ORIG_PASSPORT).strip()
    p["card"]     = re.sub(r"\D", "", (data.get("card") or _ORIG_CARD)) or _ORIG_CARD

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
        rub_str = f"{rub:,}".replace(",", " ")
        return f"{rub_str},{kop:02d} \u20bd"

    total_amt  = data.get("amount") or data.get("total_amount") or "2670"
    commission = data.get("commission") if data.get("commission") not in (None, "") else "150"

    p["amount_total"] = _fmt_amount(total_amt, _ORIG_TOTAL.strip())
    p["commission"]   = _fmt_amount(commission, _ORIG_COMM.strip())
    try:
        t = float(str(total_amt).replace(" ", "").replace(",", "."))
        c = float(str(commission).replace(" ", "").replace(",", "."))
        no_comm_val = max(0.0, t - c)
        p["amount_no_comm"] = _fmt_amount(no_comm_val, p["amount_total"])
    except Exception:
        p["amount_no_comm"] = p["amount_total"]

    return p


# ─── Замена TJ/Tj блока (структура как в mPDF-шаблоне) ───────────────────────

def _kern_gap(kern: int) -> bytes:
    """Между сегментами в шаблоне: ` -9(\\x00 ) ` / ` -4(\\x00 ) ` (пробел U+0020 в UTF-16BE)."""
    return b" -" + str(kern).encode("ascii") + b"(\x00 ) "


def _build_header_tj(text: str, ctx: OtpCardOrigContext) -> Optional[bytes]:
    """Заголовок в шаблоне — один литерал `(…) Tj`, не массив TJ."""
    enc = ctx.encode_string(text, "F3")
    if enc is None:
        return None
    return b"(" + enc + b") Tj"


def _build_tj_segments(parts: List[str], kern: int, tag: str, ctx: OtpCardOrigContext) -> Optional[bytes]:
    """`[(p0) -K(\\x00 ) (p1) … ] TJ` как в mPDF."""
    if not parts:
        return None
    out = bytearray(b"[")
    for i, p in enumerate(parts):
        enc = ctx.encode_string(p, tag)
        if enc is None:
            return None
        out.extend(b"(" + enc + b")")
        if i + 1 < len(parts):
            out.extend(_kern_gap(kern))
    out.extend(b"] TJ")
    return bytes(out)


def _parse_date_full(date_full: str) -> Optional[Tuple[str, str, str]]:
    """`DD.MM.YYYY HH:MM:SS (ЗОНА)` → дата, время, метка внутри скобок (напр. МСК)."""
    m = re.match(
        r"^(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})\s+\(([^)]+)\)\s*$",
        date_full.strip(),
    )
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _build_date_tj(date_full: str, ctx: OtpCardOrigContext) -> Optional[bytes]:
    """Как в mPDF: `[(дата) -9( ) (время) -9( ) (\\(ЗОНА\\)) ] TJ` — скобки зоны в UTF-16."""
    parsed = _parse_date_full(date_full)
    if not parsed:
        return None
    d, t, zone = parsed
    bsl = ctx.encode_string("\\", "F2")
    lpar = ctx.encode_string("(", "F2")
    rpar = ctx.encode_string(")", "F2")
    zm = ctx.encode_string(zone, "F2")
    ed = ctx.encode_string(d, "F2")
    et = ctx.encode_string(t, "F2")
    if not all((bsl, lpar, rpar, zm, ed, et)):
        return None
    zone_bytes = b"(" + bsl + lpar + zm + bsl + rpar + b")"
    parts_bytes = [b"(" + ed + b")", b"(" + et + b")", zone_bytes]
    out = bytearray(b"[")
    for i, lit in enumerate(parts_bytes):
        out.extend(lit)
        if i + 1 < len(parts_bytes):
            out.extend(_kern_gap(9))
    out.extend(b"] TJ")
    return bytes(out)


def _money_tj_parts(amount_with_ruble: str) -> List[str]:
    """`2 520,00 ₽` → [\"2\", \"520,00\", \"₽\"]; `150,00 ₽` → [\"150,00\", \"₽\"]."""
    s = amount_with_ruble.strip().replace("\u00a0", " ")
    s = s.replace("₽", "\u20bd").strip()
    if s.endswith("\u20bd"):
        body = s[: -len("\u20bd")].strip()
    else:
        body = s
    if not body:
        return ["\u20bd"]
    chunks = body.split()
    if not chunks:
        return ["\u20bd"]
    return chunks + ["\u20bd"]


_TJ_PAT_LITERAL = rb"\[\s*\(.*?\)\s*\]\s*TJ"
_TJ_RE = re.compile(_TJ_PAT_LITERAL, re.S)
_TJ_OR_TJ_RE = re.compile(rb"(?:\[(?:[^\[\]]|\\.)*?\]|\((?:[^()\\]|\\.)*?\))\s*T[Jj]", re.S)


def _decode_literal(literal: bytes) -> bytes:
    """Декодирует `(...)` → raw bytes, обрабатывая `\\(`, `\\)`, `\\\\`."""
    if not (literal.startswith(b"(") and literal.endswith(b")")):
        return literal
    body = literal[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        b = body[i]
        if b == 0x5C and i + 1 < len(body):  # backslash escape
            nxt = body[i + 1]
            unescape = {0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C, 0x6E: 0x0A,
                        0x72: 0x0D, 0x62: 0x08, 0x66: 0x0C, 0x74: 0x09}
            if nxt in unescape:
                out.append(unescape[nxt])
                i += 2
                continue
        out.append(b)
        i += 1
    return bytes(out)


def _decode_tj_text(tj_or_tj_block: bytes) -> str:
    """Извлекает все литералы из TJ/Tj блока и декодирует utf-16be → text."""
    raw_bytes = bytearray()
    if tj_or_tj_block.rstrip().endswith(b"Tj"):
        # одиночный (...) Tj
        m = re.search(rb"\((.*)\)\s*Tj", tj_or_tj_block, re.S)
        if m:
            raw_bytes.extend(_decode_literal(b"(" + m.group(1) + b")"))
    else:
        # массив [ (...) N (...) ... ] TJ
        for m in re.finditer(rb"\(((?:[^()\\]|\\.)*)\)", tj_or_tj_block, re.S):
            raw_bytes.extend(_decode_literal(b"(" + m.group(1) + b")"))
    try:
        return raw_bytes.decode("utf-16be", errors="replace")
    except Exception:
        return ""


def _find_tj_blocks(cs: bytes) -> List[Tuple[int, int, str]]:
    """Возвращает список (start, end, decoded_text) всех Tj/TJ блоков."""
    out: List[Tuple[int, int, str]] = []
    for m in _TJ_OR_TJ_RE.finditer(cs):
        block = m.group(0)
        text = _decode_tj_text(block)
        out.append((m.start(), m.end(), text))
    return out


def _norm_txt(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _replace_field(
    cs: bytes,
    expected_text: str,
    new_text: str,
    tag: str,
    ctx: OtpCardOrigContext,
    font_size: float,
    *,
    field_kind: str,
    right_align: bool = False,
    right_x: float = _RIGHT_X,
) -> bytes:
    """Найти TJ/Tj блок по decoded text и заменить, сохраняя тип оператора и mPDF-структуру."""
    expected_norm = _norm_txt(expected_text)
    new_norm = _norm_txt(new_text)
    if expected_norm == new_norm:
        return cs

    target = None
    for s, e, txt in _find_tj_blocks(cs):
        if _norm_txt(txt) == expected_norm:
            target = (s, e, txt)
            break
    if target is None:
        logger.warning(f"OTP card: TJ block not found for {expected_text!r}")
        return cs

    s, e, _ = target
    new_block: Optional[bytes] = None

    if field_kind == "header":
        new_block = _build_header_tj(new_text, ctx)
    elif field_kind == "date":
        new_block = _build_date_tj(new_text, ctx)
    elif field_kind == "words":
        parts = [w for w in re.split(r"\s+", new_text.strip()) if w]
        new_block = _build_tj_segments(parts, 9, tag, ctx) if parts else None
    elif field_kind == "single":
        new_block = _build_tj_segments([new_text], 9, tag, ctx)
    elif field_kind == "money_f2":
        new_block = _build_tj_segments(_money_tj_parts(new_text), 9, tag, ctx)
    elif field_kind == "money_f3":
        new_block = _build_tj_segments(_money_tj_parts(new_text), 4, tag, ctx)
    else:
        logger.warning(f"OTP card: unknown field_kind {field_kind!r}")
        return cs

    if new_block is None:
        logger.warning(f"OTP card: cannot build block for {new_text!r} ({field_kind})")
        return cs

    cs2 = cs[:s] + new_block + cs[e:]

    if right_align:
        win_start = max(0, s - 200)
        delta = len(new_block) - (e - s)
        win = cs2[win_start : s + delta]
        m_td = None
        for m in re.finditer(rb"([\-0-9.]+)\s+([\-0-9.]+)\s+Td", win):
            m_td = m
        if m_td:
            new_w = ctx.text_width(new_text, font_size, tag) + max(0, len(new_text) - 1) * _TC
            new_x = right_x - new_w
            old_y = m_td.group(2).decode()
            new_td = f"{new_x:.3f} {old_y} Td".encode("ascii")
            abs_start = win_start + m_td.start()
            abs_end = win_start + m_td.end()
            cs2 = cs2[:abs_start] + new_td + cs2[abs_end:]
    return cs2


def _apply_replacements(cs: bytes, p: Dict, ctx: OtpCardOrigContext) -> bytes:
    cs = _replace_field(
        cs, _ORIG_HEADER, p["header"], "F3", ctx, 20, field_kind="header"
    )
    cs = _replace_field(
        cs, _ORIG_DATE_FULL, p["date_full"], "F2", ctx, 12, field_kind="date"
    )
    cs = _replace_field(
        cs, _ORIG_SENDER, p["sender"], "F2", ctx, 12, field_kind="words", right_align=True
    )
    cs = _replace_field(
        cs, _ORIG_PASSPORT, p["passport"], "F2", ctx, 12, field_kind="words", right_align=True
    )
    cs = _replace_field(
        cs, _ORIG_CARD, p["card"], "F2", ctx, 12, field_kind="single", right_align=True
    )
    cs = _replace_field(
        cs,
        _ORIG_AMOUNT_NC.strip(),
        p["amount_no_comm"],
        "F2",
        ctx,
        12,
        field_kind="money_f2",
        right_align=True,
    )
    cs = _replace_field(
        cs,
        _ORIG_COMM.strip(),
        p["commission"],
        "F2",
        ctx,
        12,
        field_kind="money_f2",
        right_align=True,
    )
    cs = _replace_field(
        cs,
        _ORIG_TOTAL.strip(),
        p["amount_total"],
        "F3",
        ctx,
        24,
        field_kind="money_f3",
        right_align=True,
    )
    return cs


# ─── Метаданные ──────────────────────────────────────────────────────────────

def _patch_metadata(pdf: bytes, date_time: str) -> bytes:
    try:
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})", date_time.strip())
        if not m:
            return pdf
        d, mo, y, hh, mm, ss = m.groups()
        new_date = f"D:{y}{mo}{d}{hh}{mm}{ss}+03'00'"
        pdf = re.sub(rb"/CreationDate\(D:\d{14}[^)]*\)",
                     f"/CreationDate({new_date})".encode("latin1"), pdf)
        pdf = re.sub(rb"/ModDate\(D:\d{14}[^)]*\)",
                     f"/ModDate({new_date})".encode("latin1"), pdf)
        return pdf
    except Exception as e:
        logger.warning(f"OTP card meta patch error: {e}")
        return pdf


# ─── Главная функция orig-mode ───────────────────────────────────────────────

def _try_orig_mode(prepared: Dict) -> Optional[bytes]:
    ctx = OtpCardOrigContext()
    if not ctx.load(ORIG_TEMPLATE):
        return None

    # Проверка глифов
    f2_texts = [
        prepared["date_full"], prepared["sender"], prepared["passport"],
        prepared["card"], prepared["amount_no_comm"], prepared["commission"],
    ]
    f3_texts = [prepared["header"], prepared["amount_total"]]

    ok2, miss2 = ctx.can_render("F2", *f2_texts)
    ok3, miss3 = ctx.can_render("F3", *f3_texts)
    if not (ok2 and ok3):
        logger.info(
            f"OTP card orig-mode: missing chars F2={miss2!r} F3={miss3!r} → fallback не реализован"
        )
        return None

    cs_new = _apply_replacements(ctx.cs_dec, prepared, ctx)
    pdf = bytearray(ctx.pdf_bytes)
    orig_comp_len = len(ctx.cs_raw)

    if cs_new == ctx.cs_dec:
        result = bytes(pdf)
    else:
        new_compressed = _pad_to_compressed_size(cs_new, orig_comp_len)
        if new_compressed is not None and len(new_compressed) == orig_comp_len:
            pdf[ctx.cs_cs:ctx.cs_ce] = new_compressed
            result = bytes(pdf)
            logger.info(f"OTP card orig-mode (in-place): {len(result)} B")
        else:
            cs_compressed = zlib.compress(cs_new, 9)
            tmp = _replace_stream_raw(bytes(ctx.pdf_bytes), ctx.cs_xref, cs_compressed)
            result = tmp
            logger.warning(
                f"OTP card orig-mode (xref-replace, length differs): {len(result)} B "
                f"vs orig {len(ctx.pdf_bytes)} B"
            )

    if prepared["date_time"] != _ORIG_DATE_TIME:
        result = _patch_metadata(result, prepared["date_time"])

    # sanity check
    try:
        d = fitz.open(stream=result, filetype="pdf")
        _ = d[0].get_text()
        d.close()
    except Exception as e:
        logger.warning(f"OTP card orig-mode produced invalid PDF: {e}")
        return None
    return result


# ─── Public API ─────────────────────────────────────────────────────────────

def create_otp_card_stealth(data: Dict) -> Optional[bytes]:
    """Главная функция: data → bytes PDF.

    Поля data:
      receipt_num    («99161372» — 8+ цифр)
      date_time      («DD.MM.YYYY HH:MM:SS»)
      sender         («АНТОН ИГОРЕВИЧ Ш******» или ФИО)
      passport       («5*** *****1»)
      card           (16 цифр)
      amount         (число — итоговая сумма перевода)
      commission     (число — комиссия)
    """
    p = _prepare(data)
    try:
        pdf = _try_orig_mode(p)
        if pdf is not None:
            logger.info(f"✅ OTP card: orig-mode OK ({len(pdf)} B)")
            return pdf
    except Exception as e:
        logger.error(f"OTP card orig-mode error: {e}", exc_info=True)
    return None
