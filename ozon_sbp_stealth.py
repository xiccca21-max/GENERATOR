"""
OZON SBP STEALTH GENERATOR (orig-mode like T-Bank SBP)
═══════════════════════════════════════════════════════════════════════════════

Стратегия:
  1. Загружаем эталонный шаблон (templates/Ozon_sbp_original.pdf).
  2. Декодируем content-stream, находим текстовые блоки оригинальных значений
     (дата, сумма, ФИО, телефон, банк, ID, и т.д.).
  3. Заменяем char-by-char Tj-операторы (Skia/PDF формат) на новые CIDs +
     корректные advance-Td. Для right-aligned полей пересчитываем Tm.x.
  4. Перепаковываем content-stream: приоритет — in-place с тем же размером
     сжатого потока (байт-в-байт как шаблон 64168 B); иначе xref-rebuild.
  5. Патчим metadata: /CreationDate, /ModDate под пользовательскую дату.

Все шрифты остаются нетронутыми (orig FontFile2). В orig-mode при удачном
padding структура PDF совпадает с шаблоном по размеру файла.
"""

import os
import re
import zlib
import random
import logging
from typing import Dict, List, Optional, Tuple

from ozon_orig_mode import OzonOrigContext

logger = logging.getLogger(__name__)

ORIG_TEMPLATE = "templates/Ozon_sbp_original.pdf"
UNLOCKED_TEMPLATE = "templates/Ozon_sbp_unlocked.pdf"

# Точный размер эталонного шаблона (≈62.66 KiB) — при in-place stream подмене выход совпадает.
_TARGET_PDF_SIZE_ORIG = 64168

# ─── Оригинальные значения шаблона ──────────────────────────────────────────
_ORIG_DATE_TIME = "05.05.2026 23:40"
_ORIG_AMOUNT    = "10 000 ₽"
_ORIG_COMMISSION = "Без комиссии"
_ORIG_RECEIVER  = "Евгений Константинович Т."
_ORIG_PHONE     = "+7 (983) 625-36-76"
_ORIG_BANK      = "Сбербанк"
_ORIG_SENDER    = "Владимир Иванович Н."
_ORIG_SBP_ID    = "B61252040439560L0B10170011750703"

# Правый край колонки значений (рассчитан из orig: x_бanк + width(Сбербанк) ≈ 351.29)
_RIGHT_X = 351.286

# Размеры шрифтов
_FS_REG_MAIN = 14.0   # большинство строк
_FS_BOLD_BIG = 24.0   # "10 000 ₽" сверху


# ─── Вспомогательные ────────────────────────────────────────────────────────

def _hex_cid(cid: int) -> str:
    return f"{cid:04X}"


def _fmt_num(x: float) -> str:
    """Форматирует число в стиле Skia/PDF (как в оригинале)."""
    if x == 0:
        return "0"
    s = f"{x:.7f}"
    s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def _build_text_block(cids: List[int], advances: List[float]) -> bytes:
    """Создаёт байт-последовательность char-by-char text:
        <C1> Tj
        adv1 0 Td <C2> Tj
        adv2 0 Td <C3> Tj
        ...
    advance для последнего символа не используется (len(advances) >= len(cids)-1).
    """
    if not cids:
        return b""
    parts = [f"<{_hex_cid(cids[0])}> Tj".encode("latin1")]
    for i, cid in enumerate(cids[1:]):
        adv = advances[i] if i < len(advances) else 0
        parts.append(f"\n{_fmt_num(adv)} 0 Td <{_hex_cid(cid)}> Tj".encode("latin1"))
    return b"".join(parts)


def _find_obj_pos(pdf: bytes, xref: int) -> Tuple[int, int]:
    pos = pdf.find(f"\n{xref} 0 obj".encode())
    if pos < 0:
        pos = pdf.find(f"{xref} 0 obj".encode())
    if pos < 0:
        raise ValueError(f"obj {xref} not found")
    end = pdf.find(b"endobj", pos) + len(b"endobj")
    return pos, end


def _replace_stream_raw(pdf: bytes, xref: int, new_stream: bytes) -> bytes:
    """Подменить тело stream-объекта, обновив /Length (как otp_sbp_stealth._replace_stream)."""
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


def _pad_to_compressed_size(stream: bytes, target_size: int) -> Optional[bytes]:
    """Сжатый поток ровно target_size байт — in-place подмена без xref-rebuild."""
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


def _force_pdf_size(pdf: bytes, target: int = _TARGET_PDF_SIZE_ORIG) -> bytes:
    """Доп. padding orphan-stream до target, если PDF меньше (как otp_sbp_stealth)."""
    if len(pdf) >= target:
        if len(pdf) > target:
            logger.warning(f"Ozon PDF {len(pdf)} B > target {target} B, cannot shrink")
        return pdf

    max_num = 0
    for m in re.finditer(rb"\n(\d+)\s+0\s+obj", pdf):
        n = int(m.group(1))
        if n > max_num:
            max_num = n
    new_num = max_num + 1
    last_end = pdf.rfind(b"endobj") + len(b"endobj")
    head = pdf[:last_end]
    tail = pdf[last_end:]

    def build_obj(stream_len: int) -> bytes:
        return (
            f"\n{new_num} 0 obj\n<</Length {stream_len}>>\nstream\n".encode("latin1")
            + b"\x00" * stream_len
            + b"\nendstream\nendobj"
        )

    deficit = target - len(pdf)
    stream_len = max(0, deficit - 70)
    candidate = pdf
    for _ in range(20):
        new_obj = build_obj(stream_len)
        candidate = _rebuild_xref(head + new_obj + tail, pdf)
        if len(candidate) == target:
            return candidate
        stream_len += target - len(candidate)
        if stream_len < 0:
            stream_len = 0

    logger.warning(f"_force_pdf_size: failed to reach {target} B (last={len(candidate)} B)")
    return pdf


def _build_text_block_for(text: str, ctx: OzonOrigContext, font_size: float, bold: bool = False) -> Optional[bytes]:
    cids = ctx.cids(text, bold=bold)
    if cids is None:
        return None
    advs = ctx.glyph_advances(cids, font_size, bold=bold)
    return _build_text_block(cids, advs)


def _orig_block_pattern(orig_text: str, ctx: OzonOrigContext, bold: bool = False) -> Optional[re.Pattern]:
    """Regex-паттерн для нахождения orig text-block в content stream.
    Использует CIDs orig-текста, между Tj допускает любое <num> 0 Td.
    Группа 0 = весь матч, group 1 = последний `>Tj` (для bounds).
    """
    cids = ctx.cids(orig_text, bold=bold)
    if cids is None:
        return None
    parts = [rf"<\s*0*{_hex_cid(cids[0])}\s*>\s*Tj"]
    for cid in cids[1:]:
        parts.append(rf"\s+\-?\d+\.?\d*\s+0\s+Td\s*<\s*0*{_hex_cid(cid)}\s*>\s*Tj")
    return re.compile("".join(parts).encode("latin1"), re.IGNORECASE)


def _orig_block_pattern_anchored(orig_text: str, ctx: OzonOrigContext, font_size: float, bold: bool = False) -> Optional[re.Pattern]:
    """Как _orig_block_pattern, но привязан к конкретному `/Fn <size> Tf … Tm …` префиксу.
    Это критично для случая, когда F4 и F5 — subset одного шрифта Skia с
    одинаковыми CIDs для общих символов (например '10 000 ₽' встречается и
    как 24-pt bold, и как 14-pt regular — без анкоринга regex матчит оба).
    """
    cids = ctx.cids(orig_text, bold=bold)
    if cids is None:
        return None
    fn = "F5" if bold else "F4"
    fs_pat = re.escape(_fmt_num(font_size))
    inner = [rf"<\s*0*{_hex_cid(cids[0])}\s*>\s*Tj"]
    for cid in cids[1:]:
        inner.append(rf"\s+\-?\d+\.?\d*\s+0\s+Td\s*<\s*0*{_hex_cid(cid)}\s*>\s*Tj")
    pattern = (
        rf"/{fn}\s+{fs_pat}\s+Tf"
        rf"[\s\S]*?Tm\s*"
        rf"({''.join(inner)})"
    )
    return re.compile(pattern.encode("latin1"), re.IGNORECASE)


# ─── Поиск Tm перед text-block ──────────────────────────────────────────────

_TM_PAT = re.compile(rb"(\-?\d+\.?\d*)\s+(\-?\d+\.?\d*)\s+(\-?\d+\.?\d*)\s+(\-?\d+\.?\d*)\s+(\-?\d+\.?\d*)\s+(\-?\d+\.?\d*)\s+Tm")


def _find_preceding_tm(cs: bytes, block_start: int) -> Optional[Tuple[int, int, Tuple[float, ...]]]:
    """Возвращает (tm_start, tm_end, (a,b,c,d,e,f)) ближайшего Tm ДО block_start."""
    last = None
    for m in _TM_PAT.finditer(cs[:block_start]):
        last = m
    if not last:
        return None
    vals = tuple(float(last.group(i)) for i in range(1, 7))
    return last.start(), last.end(), vals


def _replace_tm_x(cs: bytes, tm_start: int, tm_end: int, tm_vals: Tuple[float, ...], new_x: float) -> Tuple[bytes, int]:
    """Заменяет Tm-оператор на новый с другим x. Возвращает (cs, delta_len)."""
    a, b, c, d, _e, f = tm_vals
    new_tm = f"{_fmt_num(a)} {_fmt_num(b)} {_fmt_num(c)} {_fmt_num(d)} {_fmt_num(new_x)} {_fmt_num(f)} Tm".encode("latin1")
    delta = len(new_tm) - (tm_end - tm_start)
    cs = cs[:tm_start] + new_tm + cs[tm_end:]
    return cs, delta


# ─── Замена text-block в content stream ─────────────────────────────────────

def _replace_block(
    cs: bytes,
    orig_text: str,
    new_text: str,
    ctx: OzonOrigContext,
    font_size: float = _FS_REG_MAIN,
    bold: bool = False,
    right_align: bool = False,
    right_x: float = _RIGHT_X,
    occurrence: int = 0,
) -> Optional[bytes]:
    """Найти orig text-block и заменить его на new_text.
    occurrence=0 — первое вхождение, 1 — второе и т.д."""
    if orig_text == new_text:
        # Текст совпадает с эталоном → не трогаем поток (сохраняем оригинальные
        # advances/kerning Skia и Tm-операторы, которые могут отличаться от /W).
        return cs

    # Anchored pattern: привязан к конкретному `/Fn <size> Tf … Tm` префиксу,
    # чтобы один и тот же текст в bold (24pt F5) и reg (14pt F4) не путались.
    pat = _orig_block_pattern_anchored(orig_text, ctx, font_size, bold=bold)
    if pat is None:
        logger.warning(f"Ozon: cannot encode orig text {orig_text!r}")
        return None
    matches = list(pat.finditer(cs))
    if occurrence >= len(matches):
        logger.warning(
            f"Ozon: anchored block not found {orig_text!r} fs={font_size} bold={bold} (occ {occurrence}, got {len(matches)})"
        )
        return None
    m = matches[occurrence]
    blk_start, blk_end = m.start(1), m.end(1)

    new_block = _build_text_block_for(new_text, ctx, font_size, bold=bold)
    if new_block is None:
        logger.warning(f"Ozon: cannot encode new text {new_text!r}")
        return None

    # 1) Подмена text-block
    cs2 = cs[:blk_start] + new_block + cs[blk_end:]

    # 2) Если right_align — пересчитать Tm.x ближайшего Tm
    if right_align:
        # Note: после замены blk_start не меняется (Tm раньше)
        tm_info = _find_preceding_tm(cs2, blk_start)
        if tm_info is None:
            logger.warning(f"Ozon: no preceding Tm before block for {orig_text!r}")
            return cs2
        tm_start, tm_end, tm_vals = tm_info
        new_w = ctx.text_width(new_text, font_size, bold=bold)
        new_x = right_x - new_w
        cs2, _ = _replace_tm_x(cs2, tm_start, tm_end, tm_vals, new_x)
    return cs2


# ─── Подготовка данных ──────────────────────────────────────────────────────

def _prepare(data: Dict) -> Dict:
    p: Dict[str, str] = {}

    date = (data.get("date") or "").strip()
    time = (data.get("time") or "").strip()
    if not date:
        date = _ORIG_DATE_TIME.split()[0]
    if not time:
        time = _ORIG_DATE_TIME.split()[1]
    p["date_time"] = f"{date} {time}"
    p["date"] = date
    p["time"] = time

    raw_amount = (data.get("amount") or "").strip()
    if not raw_amount:
        raw_amount = "10 000"
    # NBSP → space; убираем "₽" если пользователь его прислал
    raw_amount = raw_amount.replace("\u00a0", " ").replace("₽", "").strip()
    raw_amount = re.sub(r"\s+", " ", raw_amount)
    # Чисто число → группировка как в эталоне "10 000" (иначе bold-строка слипается)
    compact = raw_amount.replace(" ", "")
    if compact.isdigit():
        raw_amount = f"{int(compact):,}".replace(",", " ")
    p["amount_no_ruble"] = raw_amount
    p["amount_with_ruble"] = f"{raw_amount} ₽"

    p["commission"] = (data.get("commission") or "Без комиссии").strip()
    p["receiver"] = (data.get("receiver") or _ORIG_RECEIVER).strip()
    p["phone"] = (data.get("phone") or _ORIG_PHONE).strip()
    p["bank_name"] = (data.get("bank_name") or _ORIG_BANK).strip()
    p["sender"] = (data.get("sender") or _ORIG_SENDER).strip()

    sbp_id = (data.get("sbp_id") or "").strip()
    if not sbp_id:
        # Если все остальные поля равны эталонным — отдаём orig ID, чтобы итоговый
        # PDF был байт-в-байт идентичен шаблону (включая Skia-кернинг). Иначе
        # генерируем случайный ID из символов orig subset (0-9 + B + L).
        defaults_match = (
            p["date_time"] == _ORIG_DATE_TIME
            and p["amount_with_ruble"] == _ORIG_AMOUNT
            and p["commission"] == _ORIG_COMMISSION
            and p["receiver"] == _ORIG_RECEIVER
            and p["phone"] == _ORIG_PHONE
            and p["bank_name"] == _ORIG_BANK
            and p["sender"] == _ORIG_SENDER
        )
        if defaults_match:
            sbp_id = _ORIG_SBP_ID
        else:
            sbp_id = "B" + "".join(random.choice("0123456789BL") for _ in range(31))
    p["sbp_id"] = sbp_id

    return p


# ─── XREF rebuild ───────────────────────────────────────────────────────────

def _replace_stream_and_rebuild(pdf: bytes, cs_xref: int, cs_cs: int, cs_ce: int, new_dec: bytes) -> bytes:
    """Заменяет тело stream на новые сжатые байты и полностью пересобирает xref."""
    new_raw = zlib.compress(new_dec, level=9)

    # Найти границы /Length в obj-словаре
    obj_start = pdf.rfind(b"obj", 0, cs_cs)
    if obj_start < 0:
        raise ValueError("Cannot find obj keyword for content stream")
    obj_dict_start = obj_start + 3
    stream_kw = pdf.find(b"stream", obj_dict_start)
    if stream_kw < 0:
        raise ValueError("Cannot find 'stream' keyword")
    obj_dict = pdf[obj_dict_start:stream_kw]

    m = re.search(rb"/Length\s+(\d+)", obj_dict)
    if not m:
        raise ValueError("Cannot find /Length in stream dict")
    new_length_str = str(len(new_raw)).encode("ascii")
    new_obj_dict = obj_dict[:m.start(1)] + new_length_str + obj_dict[m.end(1):]
    delta_dict = len(new_obj_dict) - len(obj_dict)

    # Сборка нового PDF (до xref)
    head = pdf[:obj_dict_start] + new_obj_dict + pdf[stream_kw:cs_cs] + new_raw

    # Найти что дальше за стримом
    after_stream = pdf[cs_ce:]
    head = head + after_stream

    return _rebuild_xref(head, pdf)


def _rebuild_xref(pdf_no_xref: bytes, original_pdf: bytes) -> bytes:
    """Пересобирает xref+trailer для модифицированного PDF, сохраняя ID/Info/Root."""
    # Убрать оригинальный xref и trailer
    xref_pos = pdf_no_xref.rfind(b"\nxref\n")
    if xref_pos < 0:
        xref_pos = pdf_no_xref.rfind(b"xref\n")
    if xref_pos < 0:
        raise ValueError("xref not found in modified PDF")
    body = pdf_no_xref[:xref_pos]
    if not body.endswith(b"\n"):
        body += b"\n"

    # Trailer оригинала
    orig_trailer_start = original_pdf.rfind(b"trailer")
    if orig_trailer_start < 0:
        raise ValueError("orig trailer not found")
    trailer_text = original_pdf[orig_trailer_start:]
    # Убрать startxref ... %%EOF
    eof_idx = trailer_text.find(b"startxref")
    if eof_idx > 0:
        trailer_dict = trailer_text[:eof_idx].rstrip()
    else:
        trailer_dict = trailer_text.rstrip()

    # Найти все obj в body
    obj_pat = re.compile(rb"\n(\d+)\s+0\s+obj\b")
    objs: Dict[int, int] = {}
    for m in obj_pat.finditer(body):
        n = int(m.group(1))
        objs[n] = m.start() + 1
    if not objs:
        # Попробовать без \n префикса (начало файла)
        for m in re.finditer(rb"(\d+)\s+0\s+obj\b", body):
            n = int(m.group(1))
            objs.setdefault(n, m.start())

    if not objs:
        raise ValueError("no obj found in body")
    max_obj = max(objs.keys())

    # Построить xref
    xref_lines = [b"xref\n", f"0 {max_obj + 1}\n".encode("ascii"), b"0000000000 65535 f \n"]
    for i in range(1, max_obj + 1):
        if i in objs:
            xref_lines.append(f"{objs[i]:010d} 00000 n \n".encode("ascii"))
        else:
            xref_lines.append(b"0000000000 00000 f \n")
    xref_blob = b"".join(xref_lines)
    xref_offset = len(body)

    # Обновить /Size в trailer
    trailer_bytes = trailer_dict
    trailer_bytes = re.sub(rb"/Size\s+\d+", f"/Size {max_obj + 1}".encode("ascii"), trailer_bytes)

    out = body + xref_blob + trailer_bytes + f"\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    return out


# ─── Metadata patching ──────────────────────────────────────────────────────

def _patch_metadata(pdf: bytes, date_iso: str, time_str: str) -> bytes:
    """Патчит /CreationDate и /ModDate в Info dict.
    date_iso: YYYY-MM-DD, time_str: HH:MM"""
    try:
        y, mo, d = date_iso.split("-")
    except Exception:
        return pdf
    h, mi = (time_str.split(":") + ["00"])[:2]
    pdf_date = f"D:{y}{mo.zfill(2)}{d.zfill(2)}{h.zfill(2)}{mi.zfill(2)}00+00'00'"

    # Найти Info dict
    new_pdf = pdf
    for tag in (b"/CreationDate", b"/ModDate"):
        idx = new_pdf.find(tag)
        while idx >= 0:
            # Найти (...) после
            paren_open = new_pdf.find(b"(", idx)
            paren_close = new_pdf.find(b")", paren_open + 1) if paren_open > 0 else -1
            if paren_open > 0 and paren_close > 0 and paren_close - paren_open < 60:
                old_val = new_pdf[paren_open + 1:paren_close]
                new_val = pdf_date.encode("ascii")
                # Если длины не совпадают — добавим пробелов в конец, но только если новое короче
                if len(new_val) < len(old_val):
                    new_val = new_val + b" " * (len(old_val) - len(new_val))
                elif len(new_val) > len(old_val):
                    # Слегка обрежем секунды/zone
                    new_val = new_val[:len(old_val)]
                new_pdf = new_pdf[:paren_open + 1] + new_val + new_pdf[paren_close:]
            idx = new_pdf.find(tag, idx + 1)
    return new_pdf


# ─── Главная функция orig/unlocked mode ─────────────────────────────────────

def _try_mode(p: Dict, template_path: str) -> Optional[bytes]:
    ctx = OzonOrigContext()
    if not ctx.load(template_path):
        return None

    # Полный список текстов для проверки
    reg_texts = [
        p["date_time"], p["amount_with_ruble"], p["commission"],
        p["receiver"], p["phone"], p["bank_name"], p["sender"], p["sbp_id"],
    ]
    bold_texts = [p["amount_with_ruble"]]

    ok_r, miss_r = ctx.can_render_reg(*reg_texts)
    if not ok_r:
        logger.warning(f"Ozon: missing reg chars: {miss_r}")
        return None
    ok_b, miss_b = ctx.can_render_bold(*bold_texts)
    if not ok_b:
        logger.warning(f"Ozon: missing bold chars: {miss_b}")
        return None

    cs = ctx.cs_dec

    # Замены (порядок: сначала менять более правые/длинные/специфичные значения)
    # 1) Дата+время (Reg, left)
    cs = _replace_block(cs, _ORIG_DATE_TIME, p["date_time"], ctx,
                        font_size=_FS_REG_MAIN, bold=False, right_align=False)
    if cs is None: return None

    # 2) Большая сумма (Bold 24pt)
    cs = _replace_block(cs, _ORIG_AMOUNT, p["amount_with_ruble"], ctx,
                        font_size=_FS_BOLD_BIG, bold=True, right_align=False)
    if cs is None: return None

    # 3) Маленькая сумма (Reg 14pt). Anchored pattern привязан к "/F4 14 Tf"
    # и не пересечётся с bold-блоком "/F5 24 Tf".
    cs = _replace_block(cs, _ORIG_AMOUNT, p["amount_with_ruble"], ctx,
                        font_size=_FS_REG_MAIN, bold=False, right_align=False)
    if cs is None: return None

    # 4) Комиссия (Reg, left)
    cs = _replace_block(cs, _ORIG_COMMISSION, p["commission"], ctx,
                        font_size=_FS_REG_MAIN, bold=False, right_align=False)
    if cs is None: return None

    # 5) Получатель (Reg, RIGHT)
    cs = _replace_block(cs, _ORIG_RECEIVER, p["receiver"], ctx,
                        font_size=_FS_REG_MAIN, bold=False, right_align=True)
    if cs is None: return None

    # 6) Телефон получателя (Reg, RIGHT)
    cs = _replace_block(cs, _ORIG_PHONE, p["phone"], ctx,
                        font_size=_FS_REG_MAIN, bold=False, right_align=True)
    if cs is None: return None

    # 7) Банк получателя (Reg, RIGHT)
    cs = _replace_block(cs, _ORIG_BANK, p["bank_name"], ctx,
                        font_size=_FS_REG_MAIN, bold=False, right_align=True)
    if cs is None: return None

    # 8) Отправитель (Reg, RIGHT)
    cs = _replace_block(cs, _ORIG_SENDER, p["sender"], ctx,
                        font_size=_FS_REG_MAIN, bold=False, right_align=True)
    if cs is None: return None

    # 9) ID операции (Reg, RIGHT)
    cs = _replace_block(cs, _ORIG_SBP_ID, p["sbp_id"], ctx,
                        font_size=_FS_REG_MAIN, bold=False, right_align=True)
    if cs is None: return None

    # Перепаковка: сначала in-place при том же compressed size (байт-в-байт как шаблон)
    pdf_ba = bytearray(ctx.pdf_bytes)
    orig_comp_len = len(ctx.cs_raw)
    if cs == ctx.cs_dec:
        pdf_out = bytes(pdf_ba)
    else:
        new_compressed = _pad_to_compressed_size(cs, orig_comp_len)
        if new_compressed is not None and len(new_compressed) == orig_comp_len:
            pdf_ba[ctx.cs_cs : ctx.cs_ce] = new_compressed
            pdf_out = bytes(pdf_ba)
            logger.info("Ozon: content stream in-place (same compressed length)")
        else:
            zlib_b = zlib.compress(cs, 9)
            tmp = _replace_stream_raw(bytes(ctx.pdf_bytes), ctx.cs_xref, zlib_b)
            pdf_out = _rebuild_xref(tmp, bytes(ctx.pdf_bytes))
            logger.warning(
                f"Ozon: xref rebuild (compressed {len(zlib_b)} B vs orig {orig_comp_len} B)"
            )

    pdf = pdf_out

    # Метаданные. Real Ozon: видимое время — local MSK, а /CreationDate в /Info — UTC,
    # т.е. (visible MSK time − 3h). Если пользовательская дата совпадает с эталоном —
    # вообще не трогаем metadata, чтобы байты были как в шаблоне.
    if p["date_time"] != _ORIG_DATE_TIME:
        try:
            d, mo, y = p["date"].split(".")
            date_iso = f"{y}-{mo}-{d}"
        except Exception:
            date_iso = "2026-05-05"
        # Конверт MSK → UTC (-3h) с сохранением минут эталона (42:11)
        try:
            hh, mm = p["time"].split(":")[:2]
            utc_h = (int(hh) - 3) % 24
            utc_mm = int(mm)
            time_utc = f"{utc_h:02d}:{utc_mm:02d}"
        except Exception:
            time_utc = "20:42"
        pdf = _patch_metadata(pdf, date_iso, time_utc)
    return pdf


# ─── Public API ─────────────────────────────────────────────────────────────

def create_ozon_sbp_stealth(data: Dict) -> Optional[bytes]:
    """Главная функция: data → bytes PDF.

    Поля data:
      date         (DD.MM.YYYY)
      time         (HH:MM)
      amount       ("10 000" — без ₽)
      commission   ("Без комиссии" или "10 ₽")
      receiver     ("Имя Отчество Ф.")
      phone        ("+7 (XXX) XXX-XX-XX")
      bank_name    ("Сбербанк")
      sender       ("Имя Отчество Ф.")
      sbp_id       (32 символа, опционально; для размера файла как у шаблона
                    используйте только 0–9, B, L — как в эталонном чеке Ozon)
    """
    p = _prepare(data)
    # 1) Сначала пробуем orig-mode (точное совпадение FontFile2 с оригиналом)
    try:
        pdf = _try_mode(p, ORIG_TEMPLATE)
        if pdf is not None:
            if len(pdf) < _TARGET_PDF_SIZE_ORIG:
                pdf = _force_pdf_size(pdf, _TARGET_PDF_SIZE_ORIG)
            if len(pdf) != _TARGET_PDF_SIZE_ORIG:
                logger.warning(
                    f"Ozon SBP orig: size {len(pdf)} B (эталон {_TARGET_PDF_SIZE_ORIG} B)"
                )
            logger.info(f"✅ Ozon SBP: orig-mode OK ({len(pdf)} bytes)")
            return pdf
    except Exception as e:
        logger.warning(f"Ozon SBP orig-mode failed: {e}", exc_info=True)
    # 2) Fallback: unlocked-mode (расширенный subset GTEesti)
    if os.path.exists(UNLOCKED_TEMPLATE):
        try:
            pdf = _try_mode(p, UNLOCKED_TEMPLATE)
            if pdf is not None:
                logger.info(f"✅ Ozon SBP: unlocked-mode OK ({len(pdf)} bytes)")
                return pdf
        except Exception as e:
            logger.error(f"Ozon SBP unlocked-mode error: {e}", exc_info=True)
    return None
