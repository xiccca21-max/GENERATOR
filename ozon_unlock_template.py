"""
OZON UNLOCK TEMPLATE BUILDER

Создаёт templates/Ozon_sbp_unlocked.pdf на основе orig:
  • Сохраняет всю структуру (CIDFontType2 /CIDToGIDMap /Identity и т.д.)
  • Заменяет FontFile2 на subset Onest (Regular для F4, SemiBold для F5)
  • Перестраивает ToUnicode CMap и /W
  • Держит CID == GID (Identity), нумерация совпадает между Reg и Bold

После этого ozon_sbp_stealth.py может работать с произвольными символами.
"""

import io
import re
import zlib
import logging
from pathlib import Path
from typing import Dict, List, Tuple

import fitz
from fontTools.ttLib import TTFont
from fontTools.subset import Subsetter

logger = logging.getLogger(__name__)

ORIG = "templates/Ozon_sbp_original.pdf"
OUT  = "templates/Ozon_sbp_unlocked.pdf"
SRC_REG  = "fonts/GTEestiProDisplay-Regular.ttf"
SRC_BOLD = "fonts/GTEestiProDisplay-Bold.ttf"


# ─── Целевой subset ─────────────────────────────────────────────────────────

def _build_target_unicodes() -> List[int]:
    """Целевой список Unicode для нового subset. Нумерация определяет CID/GID."""
    chars = []
    chars.append(0x0000)  # placeholder для .notdef → CID 0
    # Базовые
    chars.append(ord(" "))
    # Цифры
    chars.extend(range(ord("0"), ord("9") + 1))
    # Пунктуация
    for c in ".,:;-+()/№*«»\u00A0\u2014":
        chars.append(ord(c))
    # ₽
    chars.append(0x20BD)
    # Latin upper / lower (для SBP ID и любых имён)
    chars.extend(range(ord("A"), ord("Z") + 1))
    chars.extend(range(ord("a"), ord("z") + 1))
    # Cyrillic upper + lower
    chars.extend(range(0x0410, 0x0450))   # А..я
    chars.append(0x0401)  # Ё
    chars.append(0x0451)  # ё
    # Удалить дубли, сохранить порядок
    seen = set()
    out = []
    for c in chars:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ─── Subset TTF и сборка структур ────────────────────────────────────────────

def _make_subset_ttf(src_path: str, unicodes: List[int]) -> bytes:
    font = TTFont(src_path)
    sub = Subsetter()
    sub.populate(unicodes=unicodes)
    sub.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


def _gid_widths(ttf_bytes: bytes) -> Tuple[Dict[int, int], int]:
    """gid → advance (font units), возвращает также upem."""
    f = TTFont(io.BytesIO(ttf_bytes))
    upem = f["head"].unitsPerEm
    hmtx = f["hmtx"].metrics
    go = f.getGlyphOrder()
    widths = {}
    for gid, name in enumerate(go):
        if name in hmtx:
            widths[gid] = int(round(hmtx[name][0]))
    return widths, upem


def _gid_to_unicode(ttf_bytes: bytes) -> Dict[int, int]:
    """gid → unicode (для ToUnicode CMap).
    fontTools getBestCmap возвращает {unicode_int: glyph_name_str}.
    Нужно использовать glyphOrder.index(name) для получения GID."""
    f = TTFont(io.BytesIO(ttf_bytes))
    cmap = f["cmap"].getBestCmap() or {}
    go = f.getGlyphOrder()
    name_to_gid = {name: gid for gid, name in enumerate(go)}
    out = {}
    for unicode_cp, glyph_name in cmap.items():
        gid = name_to_gid.get(glyph_name)
        if gid is not None:
            out[gid] = int(unicode_cp)
    return out


# ─── Построение строк PDF ────────────────────────────────────────────────────

def _build_w_array(widths: Dict[int, int]) -> str:
    """Строит компактный /W: одиночные пары → группами `gid [w1 w2 ...]`."""
    if not widths:
        return "[]"
    parts = []
    items = sorted(widths.items())
    i = 0
    while i < len(items):
        gid_start, w = items[i]
        run = [w]
        j = i + 1
        while j < len(items) and items[j][0] == items[j-1][0] + 1:
            run.append(items[j][1])
            j += 1
        if len(run) == 1:
            parts.append(f"{gid_start} [{run[0]}]")
        else:
            parts.append(f"{gid_start} [{' '.join(str(w) for w in run)}]")
        i = j
    return "[" + " ".join(parts) + "]"


def _build_tounicode_cmap(gid_to_uni: Dict[int, int], font_name: str) -> bytes:
    """Возвращает байтовый stream нового ToUnicode CMap."""
    items = sorted(gid_to_uni.items())  # by cid
    if not items:
        items = [(0, 0)]

    # bfchar блоками по 100
    chunks = []
    for i in range(0, len(items), 100):
        sub = items[i:i + 100]
        lines = [f"{len(sub)} beginbfchar"]
        for cid, uni in sub:
            lines.append(f"<{cid:04X}> <{uni:04X}>")
        lines.append("endbfchar")
        chunks.append("\n".join(lines))

    cmap = f"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo
<<  /Registry (Adobe)
/Ordering (UCS)
/Supplement 0
>> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
{chr(10).join(chunks)}
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""
    return cmap.encode("latin1")


# ─── Обновление xref-объектов в PDF ─────────────────────────────────────────

def _set_stream(doc: fitz.Document, xref: int, raw: bytes, dict_updates: Dict[str, str] = None):
    """Обновляет тело stream (raw — НЕ сжатый) и при необходимости поля dict."""
    # PyMuPDF: update_stream сжимает auto, и обновляет /Length
    doc.update_stream(xref, raw, compress=True)
    if dict_updates:
        obj = doc.xref_object(xref)
        for key, val in dict_updates.items():
            pat = re.compile(rf"/{re.escape(key)}\s+[^\s/<>]+")
            if pat.search(obj):
                obj = pat.sub(f"/{key} {val}", obj)
            else:
                obj = obj.rstrip(">").rstrip(">") + f" /{key} {val} >>"
        doc.update_object(xref, obj)


# ─── Парсинг оригинальной ToUnicode CMap (для перемаппинга CS) ──────────────

def _parse_orig_tounicode(text: str) -> Dict[int, int]:
    """{cid: unicode}."""
    res = {}
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for m in re.finditer(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk
        ):
            c0, c1, u0 = int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16)
            for i in range(c1 - c0 + 1):
                res[c0 + i] = u0 + i
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            res[int(m.group(1), 16)] = int(m.group(2), 16)
    return res


# ─── Перепрограммирование content stream под новые CID ──────────────────────

def _remap_content_stream(
    cs: bytes,
    orig_cid2uni_reg: Dict[int, int],
    new_uni2cid_reg: Dict[int, int],
    new_widths_reg: Dict[int, int],
    orig_cid2uni_bold: Dict[int, int],
    new_uni2cid_bold: Dict[int, int],
    new_widths_bold: Dict[int, int],
    upem: int,
) -> bytes:
    """Проходит по cs, находит /F4 / /F5 секции, заменяет каждую пару
    `<oldCID>Tj` (плюс предшествующий `adv 0 Td` если есть) на новые CIDs +
    пересчитанный advance."""
    cs_str = cs.decode("latin1", "replace")

    out = []
    i = 0
    current_font = "4"  # default
    current_size = 14.0

    # Token RE — берём один из: /FN N Tf, <hex>Tj, число 0 Td (text moves), "Tm", or any other char
    TF_RE = re.compile(r"/F(\d+)\s+([\d.]+)\s+Tf")
    TJ_RE = re.compile(r"<([0-9A-Fa-f]+)>\s*Tj")
    TD_RE = re.compile(r"(\-?\d+\.?\d*)\s+(\-?\d+\.?\d*)\s+Td")

    pos = 0
    n = len(cs_str)
    while pos < n:
        # Try Tf
        m = TF_RE.match(cs_str, pos)
        if m:
            current_font = m.group(1)
            current_size = float(m.group(2))
            out.append(m.group(0))
            pos = m.end()
            continue
        # Try Tj — может быть multi-CID `<02360003>Tj` (Identity-H, по 4 hex char = 1 CID)
        m = TJ_RE.match(cs_str, pos)
        if m:
            hex_str = m.group(1)
            # дополним нулями слева до длины кратной 4
            if len(hex_str) % 4 != 0:
                hex_str = hex_str.zfill(((len(hex_str) // 4) + 1) * 4)
            # parsing по 2 байта
            new_hex_parts = []
            for k in range(0, len(hex_str), 4):
                old_cid = int(hex_str[k:k + 4], 16)
                if current_font == "5":
                    uni = orig_cid2uni_bold.get(old_cid)
                    new_cid = new_uni2cid_bold.get(uni) if uni else None
                else:
                    uni = orig_cid2uni_reg.get(old_cid)
                    new_cid = new_uni2cid_reg.get(uni) if uni else None
                new_hex_parts.append(f"{new_cid:04X}" if new_cid is not None else hex_str[k:k + 4])
            out.append(f"<{''.join(new_hex_parts)}> Tj")
            pos = m.end()
            continue
        # Try Td (advance) — пересчитываем под новый GID, опираясь на ПРЕДЫДУЩИЙ Tj
        m = TD_RE.match(cs_str, pos)
        if m:
            x = float(m.group(1))
            y = float(m.group(2))
            # Если предшествующий output — Tj, можно пересчитать на основе old advance.
            # Но это сложно. Просто пересчитаем по последнему Tj.
            if y == 0 and out and out[-1].endswith("Tj"):
                last = out[-1]
                lm = re.match(r"<([0-9A-Fa-f]+)>\s*Tj", last)
                if lm:
                    hs = lm.group(1)
                    # последний CID в multi-CID — его advance нужен
                    last_cid = int(hs[-4:], 16)
                    if current_font == "5":
                        w = new_widths_bold.get(last_cid, 500)
                    else:
                        w = new_widths_reg.get(last_cid, 500)
                    new_x = w * current_size / upem
                    out.append(f"{new_x:.4f} 0 Td")
                else:
                    out.append(m.group(0))
            else:
                out.append(m.group(0))
            pos = m.end()
            continue
        # Default: пройти один символ
        out.append(cs_str[pos])
        pos += 1

    return "".join(out).encode("latin1")


def build_unlocked_template():
    """Главная функция: строит Ozon_sbp_unlocked.pdf."""
    if not Path(SRC_REG).exists():
        raise FileNotFoundError(f"missing {SRC_REG}")
    if not Path(SRC_BOLD).exists():
        raise FileNotFoundError(f"missing {SRC_BOLD}")

    unicodes = _build_target_unicodes()
    print(f"Target subset: {len(unicodes)} unicodes")

    # 1) Subset GT Eesti Pro Display для Regular и Bold
    reg_ttf = _make_subset_ttf(SRC_REG, unicodes)
    bold_ttf = _make_subset_ttf(SRC_BOLD, unicodes)
    print(f"Subset Reg TTF:  {len(reg_ttf)} B")
    print(f"Subset Bold TTF: {len(bold_ttf)} B")

    reg_widths, reg_upem = _gid_widths(reg_ttf)
    bold_widths, bold_upem = _gid_widths(bold_ttf)
    reg_gid2uni = _gid_to_unicode(reg_ttf)
    bold_gid2uni = _gid_to_unicode(bold_ttf)
    print(f"Reg upem={reg_upem}  Bold upem={bold_upem}")
    print(f"Reg gids with width:  {len(reg_widths)}")
    print(f"Bold gids with width: {len(bold_widths)}")

    # 2) Открываем orig, находим font xrefs
    doc = fitz.open(ORIG)
    f4_ff = f5_ff = None
    f4_cidfont = f5_cidfont = None
    f4_tounicode = f5_tounicode = None
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref)
        except Exception:
            continue
        if "/Type /FontDescriptor" in obj:
            m_name = re.search(r"/FontName\s*/([^\s/<>]+)", obj)
            m_ff = re.search(r"/FontFile2\s+(\d+)\s+0\s+R", obj)
            if m_name and m_ff:
                if "Regular" in m_name.group(1):
                    f4_ff = int(m_ff.group(1))
                elif "Bold" in m_name.group(1):
                    f5_ff = int(m_ff.group(1))
        elif "/Subtype /CIDFontType2" in obj:
            m_name = re.search(r"/BaseFont\s*/([^\s/<>]+)", obj)
            if m_name:
                if "Regular" in m_name.group(1):
                    f4_cidfont = xref
                elif "Bold" in m_name.group(1):
                    f5_cidfont = xref
        elif "/Type /Font" in obj and "/Subtype /Type0" in obj:
            m_base = re.search(r"/BaseFont\s*/([^\s/<>]+)", obj)
            m_tu = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
            if m_base and m_tu:
                if "Regular" in m_base.group(1):
                    f4_tounicode = int(m_tu.group(1))
                elif "Bold" in m_base.group(1):
                    f5_tounicode = int(m_tu.group(1))

    print(f"\nFound xrefs: F4 ff={f4_ff} cid={f4_cidfont} tu={f4_tounicode}")
    print(f"             F5 ff={f5_ff} cid={f5_cidfont} tu={f5_tounicode}")
    if not all([f4_ff, f5_ff, f4_cidfont, f5_cidfont, f4_tounicode, f5_tounicode]):
        raise RuntimeError("font xrefs missing")

    # 3) Заменяем FontFile2 (через update_stream)
    doc.update_stream(f4_ff, reg_ttf, compress=True)
    doc.update_stream(f5_ff, bold_ttf, compress=True)
    print(f"\nReplaced FontFile2 streams.")

    # 4) Обновляем CIDFontType2: /W array (balanced bracket удаление)
    def _remove_w(text: str) -> str:
        idx = text.find("/W")
        if idx < 0:
            return text
        # пропускаем /W и пробелы
        i = idx + 2
        while i < len(text) and text[i] in " \r\n\t":
            i += 1
        if i >= len(text) or text[i] != "[":
            return text
        depth = 0
        j = i
        while j < len(text):
            c = text[j]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        return text[:idx] + text[j:]

    for xref, widths, label in (
        (f4_cidfont, reg_widths, "Regular"),
        (f5_cidfont, bold_widths, "Bold"),
    ):
        obj = doc.xref_object(xref)
        obj = _remove_w(obj)
        # Удалить /DW тоже — поставим свой
        obj = re.sub(r"/DW\s+\-?\d+(\.\d+)?", "", obj)
        new_w = "/W " + _build_w_array(widths) + " /DW 500"
        if obj.rstrip().endswith(">>"):
            stripped = obj.rstrip()
            obj = stripped[:-2].rstrip() + f"\n  {new_w}\n>>"
        else:
            obj = obj + f"\n  {new_w}"
        doc.update_object(xref, obj)
        print(f"Updated /W for {label} CIDFontType2 (xref={xref})")

    # 5) Обновляем ToUnicode CMaps
    for xref, gid2uni, label in (
        (f4_tounicode, reg_gid2uni, "Regular"),
        (f5_tounicode, bold_gid2uni, "Bold"),
    ):
        cmap_bytes = _build_tounicode_cmap(gid2uni, label)
        doc.update_stream(xref, cmap_bytes, compress=True)
        print(f"Updated ToUnicode for {label} (xref={xref}, {len(gid2uni)} chars, {len(cmap_bytes)} B)")

    # 6) Перепрограммируем content stream — старые CIDs → новые CIDs
    # Парсим orig ToUnicode + glyph names из FontFile2 (для footer'а), потом ремапим CIDs.
    orig_doc = fitz.open(ORIG)
    orig_cid2uni_reg = _parse_orig_tounicode(
        orig_doc.xref_stream(f4_tounicode).decode("latin1", "replace")
    )
    orig_cid2uni_bold = _parse_orig_tounicode(
        orig_doc.xref_stream(f5_tounicode).decode("latin1", "replace")
    )
    # дополним из FontFile2 (orig) — там есть glyphs с именами `uniXXXX`,
    # которые не отмаплены в ToUnicode но физически прорисованы в footer
    orig_reg_ttf = orig_doc.xref_stream(f4_ff)
    orig_bold_ttf = orig_doc.xref_stream(f5_ff)
    orig_doc.close()

    def _augment_from_glyph_names(cid_to_uni: Dict[int, int], ttf_bytes: bytes):
        try:
            f = TTFont(io.BytesIO(ttf_bytes))
        except Exception:
            return
        go = f.getGlyphOrder()
        # Сначала из cmap (надёжный mapping name→unicode)
        cmap = f["cmap"].getBestCmap() or {}
        name_to_uni = {}
        for u, name in cmap.items():
            name_to_uni.setdefault(name, u)
        for gid, name in enumerate(go):
            if gid in cid_to_uni:
                continue
            uni = name_to_uni.get(name)
            if uni is None:
                m = re.match(r"^uni([0-9A-Fa-f]{4})$", name)
                if m:
                    uni = int(m.group(1), 16)
            if uni is not None:
                cid_to_uni[gid] = uni
        f.close()

    _augment_from_glyph_names(orig_cid2uni_reg, orig_reg_ttf)
    _augment_from_glyph_names(orig_cid2uni_bold, orig_bold_ttf)
    print(f"\nAfter augment: orig Reg cid2uni={len(orig_cid2uni_reg)}, Bold={len(orig_cid2uni_bold)}")

    # uni → new_cid для unlocked
    new_uni2cid_reg = {u: gid for gid, u in reg_gid2uni.items()}
    new_uni2cid_bold = {u: gid for gid, u in bold_gid2uni.items()}

    # cs page 0
    page = doc[0]
    cs_xref = page.get_contents()[0]
    cs = doc.xref_stream(cs_xref)
    cs = _remap_content_stream(
        cs,
        orig_cid2uni_reg, new_uni2cid_reg, reg_widths,
        orig_cid2uni_bold, new_uni2cid_bold, bold_widths,
        reg_upem,
    )
    doc.update_stream(cs_xref, cs, compress=True)
    print(f"\nRemapped content stream ({len(cs)} B decoded)")

    # 7) Сохраняем
    Path("templates").mkdir(exist_ok=True)
    doc.save(OUT, garbage=4, deflate=True, clean=True)
    doc.close()
    size = Path(OUT).stat().st_size
    print(f"\n✅ Wrote {OUT} ({size} B = {size/1024:.1f} KB)")


if __name__ == "__main__":
    build_unlocked_template()
