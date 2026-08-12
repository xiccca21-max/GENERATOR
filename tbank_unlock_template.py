"""
Build "unlocked" T-Bank template:
- replace subsetted embedded fonts with full TinkoffSans-Regular/Medium
- regenerate ToUnicode CMaps so every Unicode char in the full fonts
  maps back to its correct glyph for validators
- remap CIDs inside the content stream from subsetted GIDs to full-font GIDs
  so the visual rendering stays the same as in the original

Run: python tbank_unlock_template.py
Output: templates/receipt_28.04.2026_unlocked.pdf
"""

import os
import re
import zlib
import logging

from io import BytesIO

import fitz  # PyMuPDF
from fontTools.ttLib import TTFont

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("unlock")

_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ORIG_TEMPLATE     = os.path.join(_DIR, "templates", "T_original.pdf")
DEFAULT_UNLOCKED_TEMPLATE = os.path.join(_DIR, "templates", "T_original_unlocked.pdf")
ORIG_TEMPLATE     = DEFAULT_ORIG_TEMPLATE       # for backwards-compat callers
UNLOCKED_TEMPLATE = DEFAULT_UNLOCKED_TEMPLATE
FONT_REGULAR = os.path.join(_DIR, "fonts", "TinkoffSans-Regular.ttf")
FONT_MEDIUM = os.path.join(_DIR, "fonts", "TinkoffSans-Medium.ttf")


def _parse_subset_tounicode(stream_text: str) -> dict:
    """Parse PDF ToUnicode CMap, return {subset_cid: unicode_codepoint}."""
    result = {}
    bf_blocks = re.findall(
        r"beginbfrange(.*?)endbfrange", stream_text, re.S
    )
    for blk in bf_blocks:
        for m in re.finditer(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>",
            blk,
        ):
            cid_lo = int(m.group(1), 16)
            cid_hi = int(m.group(2), 16)
            uni_lo = int(m.group(3), 16)
            for i in range(cid_hi - cid_lo + 1):
                result[cid_lo + i] = uni_lo + i
    bf_chars = re.findall(
        r"beginbfchar(.*?)endbfchar", stream_text, re.S
    )
    for blk in bf_chars:
        for m in re.finditer(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk
        ):
            cid = int(m.group(1), 16)
            uni = int(m.group(2), 16)
            result[cid] = uni
    return result


# Набор символов для ToUnicode CMap (расширение поверх оригинала).
# FontFile2 теперь берётся целиком из исходного файла шрифта.
_NEEDED_UNICODES = set()

# 1) Цифры и базовая пунктуация
_NEEDED_UNICODES.update(ord(c) for c in "0123456789")
_NEEDED_UNICODES.update(ord(c) for c in " .,:;()*-+/№@_")

# 2) Полная русская кириллица
_NEEDED_UNICODES.update(range(0x0410, 0x0450))  # А-я
_NEEDED_UNICODES.update([0x0401, 0x0451])        # Ё ё

# 3) Латиница (SBP-идентификаторы)
_NEEDED_UNICODES.update(range(ord("A"), ord("Z") + 1))
_NEEDED_UNICODES.update(range(ord("a"), ord("z") + 1))

# 4) Спец-символы
_NEEDED_UNICODES.update([0x00A0, 0x20BD, 0x2116])
_NEEDED_UNICODES.update([0x2009, 0x202F])

# Medium: Итого + большая сумма
_NEEDED_UNICODES_MED = set()
_NEEDED_UNICODES_MED.update(ord(c) for c in "0123456789 .,И+-итог")
_NEEDED_UNICODES_MED.update([0x00A0, 0x2009, 0x202F, 0x20BD])

# Таблицы, отсутствующие в JasperReports-вставленных шрифтах
_TABLES_TO_DROP = [
    "cmap", "OS/2", "name", "post", "FFTM",
    "DSIG", "GSUB", "GPOS", "kern", "BASE", "JSTF",
    "fvar", "GDEF", "STAT", "gasp", "HVAR", "VHEA", "VORG",
]

# Таблицы хинтинга, которые должны присутствовать (как в оригинале банка)
_HINT_TABLES = ["cvt ", "fpgm", "prep"]


def _prepare_full_font(font_path: str, target_modified: int = None,
                       orig_hint_bytes: bytes = None):
    """Загружает полный TTF-шрифт (без сабсеттинга), копирует таблицы хинтинга
    из оригинального встроенного шрифта, устанавливает head.modified и удаляет
    лишние таблицы.

    orig_hint_bytes — сырые байты оригинального FontFile2 из шаблона, из которого
    берутся cvt/fpgm/prep (они есть в банковском шрифте 2021 года, но могут
    отсутствовать в более новой версии TTF-файла).

    Возвращает (TTFont, raw_bytes, {unicode: gid}).
    """
    fontlog = logging.getLogger("fontTools")
    prev_lvl = fontlog.level
    fontlog.setLevel(logging.ERROR)
    try:
        # recalcTimestamp=False — fontTools не перезапишет head.modified при сохранении
        f = TTFont(font_path, recalcTimestamp=False)

        # Извлекаем Unicode→GID ДО удаления cmap
        uni_to_gid: dict = {}
        cmap = f.getBestCmap()
        if cmap:
            for cp, gname in cmap.items():
                try:
                    uni_to_gid[cp] = f.getGlyphID(gname)
                except Exception:
                    pass

        # Копируем таблицы хинтинга из оригинального встроенного шрифта
        if orig_hint_bytes:
            orig_font = TTFont(BytesIO(orig_hint_bytes))
            for tbl in _HINT_TABLES:
                if tbl in orig_font and tbl not in f:
                    f[tbl] = orig_font[tbl]
                    log.info(f"  copied hinting table {tbl!r} from original")

        # Устанавливаем дату, соответствующую оригинальному шрифту в шаблоне
        if target_modified is not None:
            f["head"].modified = target_modified

        # Удаляем таблицы, которых нет в JasperReports-встроенных шрифтах
        for tbl in _TABLES_TO_DROP:
            if tbl in f:
                del f[tbl]

        bio = BytesIO()
        f.save(bio)
        return f, bio.getvalue(), uni_to_gid
    finally:
        fontlog.setLevel(prev_lvl)


def _prepare_subset_font(font_path: str, needed_unicodes: set,
                         target_modified: int = None,
                         orig_hint_bytes: bytes = None):
    """Создаёт минимальный сабсет TTF-шрифта с needed_unicodes.
    После сабсетинга копирует hinting-таблицы из оригинального встроенного
    шрифта, выставляет head.modified и удаляет лишние таблицы.
    GID-маппинг берётся из cmap сабсетированного шрифта ДО удаления cmap.
    Возвращает (TTFont, raw_bytes, {unicode: gid}).
    """
    from fontTools.subset import Subsetter, Options as SubOptions

    fontlog = logging.getLogger("fontTools")
    prev_lvl = fontlog.level
    fontlog.setLevel(logging.ERROR)
    try:
        f = TTFont(font_path, recalcTimestamp=False)

        opts = SubOptions()
        opts.recalcTimestamp = False
        opts.retain_gids = True   # сохраняем оригинальные GID как JasperReports
        subsetter = Subsetter(options=opts)
        subsetter.populate(unicodes=needed_unicodes)
        subsetter.subset(f)

        # Извлекаем Unicode→GID из cmap сабсета ДО её удаления
        uni_to_gid: dict = {}
        cmap_after = f.getBestCmap()
        if cmap_after:
            for cp, gname in cmap_after.items():
                try:
                    uni_to_gid[cp] = f.getGlyphID(gname)
                except Exception:
                    pass

        # Копируем hinting-таблицы из оригинального встроенного шрифта
        if orig_hint_bytes:
            orig_font = TTFont(BytesIO(orig_hint_bytes))
            for tbl in _HINT_TABLES:
                if tbl in orig_font and tbl not in f:
                    f[tbl] = orig_font[tbl]
                    log.info(f"  copied hinting table {tbl!r} from original")

        # Выставляем дату, как в оригинальном встроенном шрифте
        if target_modified is not None:
            f["head"].modified = target_modified

        # Удаляем таблицы, которых нет в JasperReports-встроенных шрифтах
        for tbl in _TABLES_TO_DROP:
            if tbl in f:
                del f[tbl]

        bio = BytesIO()
        f.save(bio)
        n_glyphs = len(f.getGlyphOrder())
        log.info(
            f"  subset: unicodes={len(needed_unicodes)} "
            f"glyphs={n_glyphs} bytes={len(bio.getvalue())}"
        )
        return f, bio.getvalue(), uni_to_gid
    finally:
        fontlog.setLevel(prev_lvl)


def _build_unicode_to_gid(font: TTFont) -> dict:
    cmap = font.getBestCmap()
    out = {}
    for cp, name in cmap.items():
        try:
            out[cp] = font.getGlyphID(name)
        except Exception:
            continue
    return out


def _build_tounicode_cmap(unicode_to_gid: dict) -> bytes:
    """Build PDF ToUnicode CMap exactly as OpenPDF/FontBox does:
    every glyph as an individual single-entry bfrange <GID><GID><Unicode>.
    OpenPDF NEVER uses bfchar — always bfrange, even for isolated glyphs."""
    pairs = sorted(((gid, cp) for cp, gid in unicode_to_gid.items()), key=lambda x: x[0])
    lines = [f"{len(pairs)} beginbfrange"]
    for gid, cp in pairs:
        lines.append(f"<{gid:04x}><{gid:04x}><{cp:04x}>")
    lines.append("endbfrange")
    body = "\n".join(lines)
    cmap = f"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo
<< /Registry (TTX+0)
/Ordering (T42UV)
/Supplement 0
>> def
/CMapName /TTX+0 def
/CMapType 2 def
1 begincodespacerange
<0000><FFFF>
endcodespacerange
{body}
endcmap
CMapName currentdict /CMap defineresource pop
end end
"""
    return cmap.encode("latin1")


def _build_widths_array(font: TTFont, gids: list) -> str:
    """Build PDF /W array using actual hmtx widths (units per em normalized to 1000).

    Proton Sber HARD uses ``floor(hmtx*1000/upem)`` — never round.
    """
    import math

    upem = font["head"].unitsPerEm
    hmtx = font["hmtx"].metrics  # {glyph_name: (advance_width, lsb)}
    glyph_order = font.getGlyphOrder()
    pairs = []
    for gid in gids:
        if gid >= len(glyph_order):
            continue
        gname = glyph_order[gid]
        adv = hmtx.get(gname, (1000, 0))[0]
        scaled = int(math.floor(adv * 1000 / upem))
        pairs.append((gid, scaled))
    pairs.sort()
    parts = []
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[j][0] + 1:
            j += 1
        if j == i:
            parts.append(f"{pairs[i][0]}[{pairs[i][1]}]")
        else:
            run = " ".join(str(p[1]) for p in pairs[i:j + 1])
            parts.append(f"{pairs[i][0]}[{run}]")
        i = j + 1
    return "[" + "".join(parts) + "]"


def _build_widths_from_map(widths: dict) -> str:
    """Компактный /W [...] из {cid: width} — тот же формат что _build_widths_array."""
    if not widths:
        return "[]"
    pairs = sorted(widths.items())
    parts = []
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[j][0] + 1:
            j += 1
        if j == i:
            parts.append(f"{pairs[i][0]}[{pairs[i][1]}]")
        else:
            run = " ".join(str(p[1]) for p in pairs[i:j + 1])
            parts.append(f"{pairs[i][0]}[{run}]")
        i = j + 1
    return "[" + "".join(parts) + "]"


def _remap_content_stream(stream: bytes, remap: dict, default_gid: int = 0) -> bytes:
    """Remap each 2-byte CID in literal-string Tj/TJ arguments using `remap`.
    PDF text strings inside ( ... ) — we only swap balanced bytes between
    (... ) Tj or [(...) ...] TJ operators.
    """
    out = bytearray()
    i = 0
    n = len(stream)
    while i < n:
        b = stream[i]
        if b != 0x28:  # not '('
            out.append(b)
            i += 1
            continue
        out.append(b)
        i += 1
        body = bytearray()
        while i < n and stream[i] != 0x29:
            if stream[i] == 0x5C and i + 1 < n:
                body.append(stream[i])
                body.append(stream[i + 1])
                i += 2
            else:
                body.append(stream[i])
                i += 1
        decoded = _decode_pdf_string(bytes(body))
        if len(decoded) % 2 != 0:
            out.extend(body)
            if i < n:
                out.append(stream[i])
                i += 1
            continue
        new_decoded = bytearray()
        for k in range(0, len(decoded), 2):
            old_cid = (decoded[k] << 8) | decoded[k + 1]
            new_cid = remap.get(old_cid)
            if new_cid is None:
                new_cid = default_gid
            new_decoded.append((new_cid >> 8) & 0xFF)
            new_decoded.append(new_cid & 0xFF)
        out.extend(_encode_pdf_string(bytes(new_decoded)))
        if i < n:
            out.append(stream[i])
            i += 1
    return bytes(out)


_ESC_DEC = {
    0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C,
    0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C,
}


def _decode_pdf_string(body: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(body):
        if body[i] == 0x5C and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in _ESC_DEC:
                out.append(_ESC_DEC[nxt])
                i += 2
                continue
            if 0x30 <= nxt <= 0x37:
                num = chr(nxt)
                k = i + 2
                while k < len(body) and len(num) < 3 and 0x30 <= body[k] <= 0x37:
                    num += chr(body[k])
                    k += 1
                out.append(int(num, 8) & 0xFF)
                i = k
                continue
            if nxt in (0x0A, 0x0D):
                i += 2
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(body[i])
        i += 1
    return bytes(out)


_ESC_ENC = {
    0x28: b"\\(", 0x29: b"\\)", 0x5C: b"\\\\",
    0x0A: b"\\n", 0x0D: b"\\r", 0x09: b"\\t",
    0x08: b"\\b", 0x0C: b"\\f",
}


def _encode_pdf_string(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        out.extend(_ESC_ENC.get(b, bytes([b])))
    return bytes(out)


def _find_font_objects(doc):
    """Identify xrefs of FontFile2, ToUnicode, and CIDFont dict for F1 (Regular) and F2 (Medium)."""
    fonts = {}
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref)
        except Exception:
            continue
        if "/Type /Font" not in obj:
            continue
        m_base = re.search(r"/BaseFont\s*/([^\s/<>]+)", obj)
        if not m_base:
            continue
        name = m_base.group(1)
        m_tu = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
        m_desc = re.search(r"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R\s*\]", obj)
        if not m_desc:
            continue
        cidfont_xref = int(m_desc.group(1))
        cidobj = doc.xref_object(cidfont_xref)
        m_fd = re.search(r"/FontDescriptor\s+(\d+)\s+0\s+R", cidobj)
        if not m_fd:
            continue
        fd_obj = doc.xref_object(int(m_fd.group(1)))
        m_ff = re.search(r"/FontFile2\s+(\d+)\s+0\s+R", fd_obj)
        if not m_ff:
            continue
        fonts[name.split("+")[-1]] = {
            "type0_xref": xref,
            "cidfont_xref": cidfont_xref,
            "fontdesc_xref": int(m_fd.group(1)),
            "fontfile_xref": int(m_ff.group(1)),
            "tounicode_xref": int(m_tu.group(1)) if m_tu else None,
        }
    return fonts


def _replace_w_in_cidfont_obj(obj: str, new_w: str) -> str:
    """Replace existing /W [...] in a CIDFontType2 dict with new_w (or insert).
    Handles nested brackets in W array.
    """
    idx = obj.find("/W")
    while idx >= 0:
        ahead = obj[idx + 2 : idx + 3]
        if ahead in (" ", "\t", "\n", "\r", "["):
            break
        idx = obj.find("/W", idx + 1)
    if idx < 0:
        return obj.replace(">>", f"/W {new_w} >>", 1)
    j = idx + 2
    while j < len(obj) and obj[j] not in "[":
        j += 1
    if j >= len(obj):
        return obj.replace(">>", f"/W {new_w} >>", 1)
    depth = 0
    end = j
    while end < len(obj):
        c = obj[end]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end += 1
                break
        end += 1
    return obj[:idx] + f"/W {new_w}" + obj[end:]


def _parse_xref_table(blob: bytes):
    """Parse classical PDF xref table at startxref offset. Returns (offsets dict, count, xref_offset)."""
    sx = blob.rfind(b"startxref")
    m = re.search(rb"startxref\s+(\d+)", blob[sx:])
    if not m:
        raise RuntimeError("no startxref")
    xref_off = int(m.group(1))
    body = blob[xref_off:]
    m = re.match(rb"xref\s*\n\s*(\d+)\s+(\d+)\s*\n", body)
    if not m:
        # Some bank shells point startxref a few bytes early (into ``endobj\nxref``).
        hit = body.find(b"xref\n")
        if 0 <= hit <= 32:
            xref_off += hit
            body = blob[xref_off:]
            m = re.match(rb"xref\s*\n\s*(\d+)\s+(\d+)\s*\n", body)
    if not m:
        raise RuntimeError("no xref header")
    first = int(m.group(1))
    count = int(m.group(2))
    bs = m.end()
    offsets = {}
    for i in range(count):
        line = body[bs + i * 20: bs + i * 20 + 20]
        if len(line) < 18:
            raise RuntimeError(f"short xref line {i}: {line!r}")
        off = int(line[:10])
        kind = line[17:18]
        if kind == b"n":
            offsets[first + i] = off
    return offsets, first, count, xref_off


def _find_object_end(blob: bytes, start: int) -> int:
    """Return offset right after `endobj` + trailing newlines."""
    end = blob.find(b"endobj", start)
    if end < 0:
        raise RuntimeError(f"endobj not found from {start}")
    end += len(b"endobj")
    while end < len(blob) and blob[end] in (0x0A, 0x0D):
        end += 1
    return end


def _split_object(obj_bytes: bytes):
    """Parse 'N G obj\\n<<dict>>[\\nstream\\n...endstream\\n]endobj' into structured parts.
    Returns dict with keys: prefix, dict_text, stream_data (or None), suffix.
    prefix = bytes from start through '<<dict>>' (inclusive)
    dict_text = the inner dict text (between <<...>>)
    stream_data = raw stream bytes (between 'stream\\n' and '\\nendstream') if present
    suffix = bytes after the dict/stream, including 'endobj' + trailing newlines
    """
    m = re.match(rb"\s*(\d+)\s+(\d+)\s+obj\s*", obj_bytes)
    if not m:
        raise RuntimeError(f"bad obj header: {obj_bytes[:40]!r}")
    head_end = m.end()
    if obj_bytes[head_end:head_end + 2] != b"<<":
        return None
    depth = 0
    i = head_end
    while i < len(obj_bytes):
        if obj_bytes[i:i + 2] == b"<<":
            depth += 1
            i += 2
        elif obj_bytes[i:i + 2] == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                break
        else:
            i += 1
    dict_end = i
    dict_inner = obj_bytes[head_end + 2: dict_end - 2]
    rest = obj_bytes[dict_end:]
    sm = re.match(rb"\s*stream(\r\n|\n)", rest)
    if sm:
        sdata_start = sm.end()
        es_pos = rest.find(b"\nendstream")
        if es_pos < 0:
            es_pos = rest.find(b"\rendstream")
        if es_pos < 0:
            raise RuntimeError("endstream not found")
        stream_data = rest[sdata_start:es_pos]
        after_es_idx = rest.find(b"endstream", es_pos) + len(b"endstream")
        suffix = rest[after_es_idx:]
        return {
            "header_end": head_end,
            "dict_inner": dict_inner,
            "dict_end": dict_end,
            "stream_data": stream_data,
            "suffix": suffix,
        }
    return {
        "header_end": head_end,
        "dict_inner": dict_inner,
        "dict_end": dict_end,
        "stream_data": None,
        "suffix": rest,
    }


def _patch_dict_value(dict_text: bytes, key: bytes, new_value: bytes) -> bytes:
    """Replace `/Key VALUE` in a PDF dict with `/Key new_value`. Handles both
    simple values (numbers, names) and arrays with nested brackets (like /W [...]).
    """
    idx = 0
    klen = len(key)
    while True:
        idx = dict_text.find(key, idx)
        if idx < 0:
            return dict_text + b" " + key + b" " + new_value
        nxt = dict_text[idx + klen: idx + klen + 1]
        if not nxt or nxt in b" \t\r\n[":
            break
        idx += klen
    val_start = idx + klen
    while val_start < len(dict_text) and dict_text[val_start: val_start + 1] in b" \t\r\n":
        val_start += 1
    if val_start >= len(dict_text):
        return dict_text + b" " + key + b" " + new_value
    if dict_text[val_start: val_start + 1] == b"[":
        depth = 0
        end = val_start
        while end < len(dict_text):
            c = dict_text[end: end + 1]
            if c == b"[":
                depth += 1
            elif c == b"]":
                depth -= 1
                if depth == 0:
                    end += 1
                    break
            end += 1
        return dict_text[:idx] + key + b" " + new_value + dict_text[end:]
    end = val_start
    while end < len(dict_text):
        c = dict_text[end: end + 1]
        if c in b" \t\r\n/>":
            break
        end += 1
    return dict_text[:idx] + key + b" " + new_value + dict_text[end:]


def _build_obj_stream(xref_num: int, dict_inner: bytes, stream_data: bytes) -> bytes:
    """Build a complete `N 0 obj\\n<<...>>stream\\n...\\nendstream\\nendobj\\n` block.

    OpenPDF/JasperReports writes >>stream (no separator between >> and stream keyword).
    Using the same format keeps the xref offsets correct and avoids structural validator flags.
    """
    return (
        f"{xref_num} 0 obj\n".encode()
        + b"<<" + dict_inner + b">>stream\n"
        + stream_data + b"\nendstream\nendobj\n"
    )


def _build_obj_dict(xref_num: int, dict_inner: bytes) -> bytes:
    """Build a complete `N 0 obj\\n<<...>>\\nendobj\\n` block (no stream)."""
    return (
        f"{xref_num} 0 obj\n".encode()
        + b"<<" + dict_inner + b">>"
        + b"\nendobj\n"
    )


def _make_modified_obj(orig_obj_bytes: bytes, xref_num: int, *,
                       new_stream: bytes = None, new_W: str = None,
                       new_length1: int = None) -> bytes:
    """Take original object bytes, replace stream content (if given) and patch
    /Length / /Length1 / /W in its dict, return rebuilt object bytes."""
    parts = _split_object(orig_obj_bytes)
    if parts is None:
        raise RuntimeError("cannot parse obj")
    dict_inner = parts["dict_inner"]
    if new_stream is not None:
        dict_inner = _patch_dict_value(dict_inner, b"/Length", str(len(new_stream)).encode())
        if new_length1 is not None:
            dict_inner = _patch_dict_value(dict_inner, b"/Length1", str(new_length1).encode())
        return _build_obj_stream(xref_num, dict_inner, new_stream)
    if new_W is not None:
        dict_inner = _patch_dict_value(dict_inner, b"/W", new_W.encode("latin1"))
    if parts["stream_data"] is not None:
        return _build_obj_stream(xref_num, dict_inner, parts["stream_data"])
    return _build_obj_dict(xref_num, dict_inner)


def build_unlocked_template(orig_path: str = None, out_path: str = None,
                              unicodes_reg=None, unicodes_med=None):
    """Build unlocked variant of an arbitrary T-Bank template.

    orig_path / out_path по умолчанию — карта (T_original.pdf → T_original_unlocked.pdf).
    unicodes_reg / unicodes_med — кастомный subset (для меньшего размера файла).
    """
    orig_path = orig_path or DEFAULT_ORIG_TEMPLATE
    out_path  = out_path  or DEFAULT_UNLOCKED_TEMPLATE
    if unicodes_reg is None:
        unicodes_reg = _NEEDED_UNICODES
    if unicodes_med is None:
        unicodes_med = _NEEDED_UNICODES_MED
    log.info(f"open: {orig_path}")
    with open(orig_path, "rb") as fp:
        orig = fp.read()
    log.info(f"orig: {len(orig)} bytes")

    doc = fitz.open(orig_path)
    fonts_meta = _find_font_objects(doc)
    log.info(f"fonts found: {list(fonts_meta.keys())}")
    f_reg = fonts_meta.get("TinkoffSans-Regular")
    f_med = fonts_meta.get("TinkoffSans-Medium")
    if not f_reg or not f_med:
        raise RuntimeError("Required TinkoffSans fonts not found in template")

    subset_to_uni_reg = _parse_subset_tounicode(
        doc.xref_stream(f_reg["tounicode_xref"]).decode("latin1", errors="replace")
    )
    subset_to_uni_med = _parse_subset_tounicode(
        doc.xref_stream(f_med["tounicode_xref"]).decode("latin1", errors="replace")
    )

    # Извлекаем head.modified из оригинальных встроенных шрифтов шаблона,
    # чтобы встроенные шрифты разблокированной версии выглядели идентично.
    orig_ff2_reg = doc.xref_stream(f_reg["fontfile_xref"])
    orig_ff2_med = doc.xref_stream(f_med["fontfile_xref"])
    tmp_reg = TTFont(BytesIO(orig_ff2_reg))
    tmp_med = TTFont(BytesIO(orig_ff2_med))
    target_modified_reg = tmp_reg["head"].modified
    target_modified_med = tmp_med["head"].modified
    log.info(f"target head.modified: reg={target_modified_reg}  med={target_modified_med}")

    # Создаём сабсет-шрифты — только нужные символы.
    # cvt/fpgm/prep берём из оригинальных встроенных шрифтов (2021/2022).
    log.info("Building Regular subset...")
    font_reg, full_reg_bytes, uni_to_gid_reg = _prepare_subset_font(
        FONT_REGULAR, unicodes_reg, target_modified_reg, orig_ff2_reg
    )
    log.info("Building Medium subset...")
    font_med, full_med_bytes, uni_to_gid_med = _prepare_subset_font(
        FONT_MEDIUM, unicodes_med, target_modified_med, orig_ff2_med
    )
    log.info(
        f"Regular subset: glyphs={len(font_reg.getGlyphOrder())} "
        f"cmap_chars={len(uni_to_gid_reg)} bytes={len(full_reg_bytes)}"
    )
    log.info(
        f"Medium subset:  glyphs={len(font_med.getGlyphOrder())} "
        f"cmap_chars={len(uni_to_gid_med)} bytes={len(full_med_bytes)}"
    )

    # Remap: оригинальные CID шаблона → GID в полном шрифте (через Unicode-мост)
    remap_reg = {sub_cid: uni_to_gid_reg[u]
                 for sub_cid, u in subset_to_uni_reg.items() if u in uni_to_gid_reg}
    remap_med = {sub_cid: uni_to_gid_med[u]
                 for sub_cid, u in subset_to_uni_med.items() if u in uni_to_gid_med}

    page = doc[0]
    cs_xref = page.get_contents()[0]
    cs_data = doc.xref_stream(cs_xref)
    new_cs = _split_and_remap_by_font(cs_data, remap_reg, remap_med)

    cmap_reg_bytes = _build_tounicode_cmap(uni_to_gid_reg)
    cmap_med_bytes = _build_tounicode_cmap(uni_to_gid_med)

    used_gids_reg = sorted(set(uni_to_gid_reg.values()))
    used_gids_med = sorted(set(uni_to_gid_med.values()))
    new_W_reg = _build_widths_array(font_reg, used_gids_reg)
    new_W_med = _build_widths_array(font_med, used_gids_med)

    doc.close()

    # ----- Byte-level reassembly preserving original layout -----
    offsets, first, count, xref_off = _parse_xref_table(orig)
    log.info(f"orig xref: first={first} count={count} xref_off={xref_off}")

    # Compute object byte ranges (sorted by offset to preserve original layout order)
    sorted_xrefs = sorted(offsets.items(), key=lambda x: x[1])
    obj_ranges = {}
    for xref_num, start in sorted_xrefs:
        end = _find_object_end(orig, start)
        obj_ranges[xref_num] = (start, end)

    # Pre-compress streams that need replacement
    new_streams = {
        f_reg["fontfile_xref"]:    (zlib.compress(full_reg_bytes, 6), len(full_reg_bytes)),
        f_med["fontfile_xref"]:    (zlib.compress(full_med_bytes, 6), len(full_med_bytes)),
        f_reg["tounicode_xref"]:   (zlib.compress(cmap_reg_bytes, 6), None),
        f_med["tounicode_xref"]:   (zlib.compress(cmap_med_bytes, 6), None),
        cs_xref:                   (zlib.compress(new_cs, 6), None),
    }

    new_dict_xrefs = {
        f_reg["cidfont_xref"]: new_W_reg,
        f_med["cidfont_xref"]: new_W_med,
    }

    # Build replacement bytes for each modified object
    replacements = {}
    for xref_num, (compressed, length1) in new_streams.items():
        s, e = obj_ranges[xref_num]
        replacements[xref_num] = _make_modified_obj(
            orig[s:e], xref_num,
            new_stream=compressed, new_length1=length1,
        )
    for xref_num, w_array in new_dict_xrefs.items():
        s, e = obj_ranges[xref_num]
        replacements[xref_num] = _make_modified_obj(
            orig[s:e], xref_num, new_W=w_array,
        )

    # Reassemble the file
    first_obj_off = sorted_xrefs[0][1]
    out = bytearray(orig[:first_obj_off])

    new_offsets = {}
    for xref_num, (s, e) in sorted(obj_ranges.items(), key=lambda kv: kv[1][0]):
        new_offsets[xref_num] = len(out)
        if xref_num in replacements:
            out.extend(replacements[xref_num])
        else:
            out.extend(orig[s:e])

    new_xref_off = len(out)

    # Build xref in original format: 'xref\n0 N\n' + 20-byte lines
    xref_lines = [b"xref\n", f"{first} {count}\n".encode()]
    for n in range(first, first + count):
        if n == 0:
            xref_lines.append(b"0000000000 65535 f \n")
        elif n in new_offsets:
            xref_lines.append(f"{new_offsets[n]:010d} 00000 n \n".encode())
        else:
            xref_lines.append(b"0000000000 00000 f \n")
    out.extend(b"".join(xref_lines))

    # Trailer — copy verbatim from orig (preserves /ID, key order, all metadata)
    trailer_pos = orig.find(b"trailer", xref_off)
    if trailer_pos < 0:
        raise RuntimeError("trailer not found")
    sx_pos = orig.find(b"startxref", trailer_pos)
    out.extend(orig[trailer_pos:sx_pos])
    out.extend(f"startxref\n{new_xref_off}\n%%EOF\n".encode())

    with open(out_path, "wb") as fp:
        fp.write(bytes(out))
    log.info(f"saved: {out_path} ({len(out)} bytes)")
    return bytes(out)


def _split_and_remap_by_font(stream: bytes, remap_reg: dict, remap_med: dict) -> bytes:
    """Walk content stream tracking active font (Tf operator) and apply correct remap.
    Active font name from operator like '/F1 12 Tf' — we use F1=regular, F2=medium, F3=other.
    """
    out = bytearray()
    cur_remap = remap_reg
    i = 0
    n = len(stream)
    while i < n:
        m = re.match(rb"/F([0-9]+)\s+[0-9.]+\s+Tf", stream[i:i + 32])
        if m:
            fid = int(m.group(1))
            if fid == 1:
                cur_remap = remap_reg
            elif fid == 2:
                cur_remap = remap_med
            else:
                cur_remap = None
            out.extend(stream[i:i + m.end()])
            i += m.end()
            continue
        if stream[i] == 0x28:
            out.append(0x28)
            i += 1
            body = bytearray()
            while i < n and stream[i] != 0x29:
                if stream[i] == 0x5C and i + 1 < n:
                    body.append(stream[i])
                    body.append(stream[i + 1])
                    i += 2
                else:
                    body.append(stream[i])
                    i += 1
            if cur_remap is None:
                out.extend(body)
            else:
                decoded = _decode_pdf_string(bytes(body))
                if len(decoded) % 2 == 0:
                    new = bytearray()
                    for k in range(0, len(decoded), 2):
                        cid = (decoded[k] << 8) | decoded[k + 1]
                        new_cid = cur_remap.get(cid, cid)
                        new.append((new_cid >> 8) & 0xFF)
                        new.append(new_cid & 0xFF)
                    out.extend(_encode_pdf_string(bytes(new)))
                else:
                    out.extend(body)
            if i < n:
                out.append(stream[i])
                i += 1
            continue
        out.append(stream[i])
        i += 1
    return bytes(out)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    # Build all three T-Bank templates (card / sbp / phone)
    targets = [
        (os.path.join(_DIR, "templates", "T_original.pdf"),
         os.path.join(_DIR, "templates", "T_original_unlocked.pdf")),
        (os.path.join(_DIR, "templates", "T_sbp_original.pdf"),
         os.path.join(_DIR, "templates", "T_sbp_unlocked.pdf")),
        (os.path.join(_DIR, "templates", "T_phone_original.pdf"),
         os.path.join(_DIR, "templates", "T_phone_unlocked.pdf")),
    ]
    for orig, out in targets:
        if os.path.exists(orig):
            log.info(f"=== building {os.path.basename(orig)} ===")
            try:
                build_unlocked_template(orig, out)
            except Exception as e:
                log.error(f"failed: {e}")
        else:
            log.warning(f"skip {orig} (not found)")
