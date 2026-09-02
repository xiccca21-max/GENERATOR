"""
T-BANK ORIG MODE — режим работы с ОРИГИНАЛЬНЫМ шаблоном без unlock'а.

Идея: если все символы пользовательских данных уже присутствуют в оригинальном
subset шрифта, то нам НЕ НУЖНО подменять FontFile2/ToUnicode/CMap. Можно
работать прямо с T_*_original.pdf и заменять только текстовые байты в content
stream. В результате 4 шрифтовых объекта (FontFile2 Regular/Medium и две
ToUnicode CMap) остаются БАЙТ-В-БАЙТ идентичными оригиналу.

Это даёт максимально близкий к настоящему чеку «отпечаток» структуры.
"""

import os
import re
import zlib
import logging
from typing import Dict, Optional, Tuple, List, Set

import fitz

logger = logging.getLogger(__name__)

_ESC = {
    0x28: b"\\(", 0x29: b"\\)", 0x5C: b"\\\\",
    0x0A: b"\\n", 0x0D: b"\\r",
    0x08: b"\\b", 0x0C: b"\\f", 0x09: b"\\t",
}

_ESC_DEC = {
    0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08, 0x66: 0x0C,
    0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C,
}


def _parse_subset_tounicode(stream_text: str) -> Dict[int, int]:
    """Parse PDF ToUnicode CMap, return {subset_cid: unicode_codepoint}."""
    result = {}
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", stream_text, re.S):
        for m in re.finditer(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk
        ):
            cid_lo = int(m.group(1), 16)
            cid_hi = int(m.group(2), 16)
            uni_lo = int(m.group(3), 16)
            for i in range(cid_hi - cid_lo + 1):
                result[cid_lo + i] = uni_lo + i
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", stream_text, re.S):
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            cid = int(m.group(1), 16)
            uni = int(m.group(2), 16)
            result[cid] = uni
    return result


def _decode_pdf_string(body: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(body):
        if body[i] == 0x5C and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in _ESC_DEC:
                out.append(_ESC_DEC[nxt]); i += 2; continue
            if 0x30 <= nxt <= 0x37:
                num = chr(nxt); k = i + 2
                while k < len(body) and len(num) < 3 and 0x30 <= body[k] <= 0x37:
                    num += chr(body[k]); k += 1
                out.append(int(num, 8) & 0xFF); i = k; continue
            if nxt in (0x0A, 0x0D):
                i += 2; continue
            out.append(nxt); i += 2; continue
        out.append(body[i]); i += 1
    return bytes(out)


def _parse_w_array(obj: str) -> Tuple[Dict[int, int], int]:
    """Parse /W array from CIDFontType2 dict; return ({cid: width}, /DW)."""
    start = obj.find("/W [")
    if start < 0:
        dw_m = re.search(r"/DW\s+(\d+)", obj)
        return {}, int(dw_m.group(1)) if dw_m else 1000
    i = start + 3
    depth = 0
    j = i
    body = ""
    while j < len(obj):
        ch = obj[j]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                body = obj[i + 1:j]
                break
        j += 1
    widths: Dict[int, int] = {}
    tokens = re.findall(r"\d+|\[|\]", body)
    k = 0
    while k < len(tokens):
        if tokens[k] in ("[", "]"):
            k += 1
            continue
        gid = int(tokens[k])
        k += 1
        if k < len(tokens) and tokens[k] == "[":
            k += 1
            run = []
            while k < len(tokens) and tokens[k] != "]":
                run.append(int(tokens[k]))
                k += 1
            if k < len(tokens) and tokens[k] == "]":
                k += 1
            for off, w in enumerate(run):
                widths[gid + off] = w
        elif k + 1 < len(tokens):
            gid_hi = int(tokens[k])
            w = int(tokens[k + 1])
            k += 2
            for g in range(gid, gid_hi + 1):
                widths[g] = w
    dw_m = re.search(r"/DW\s+(\d+)", obj)
    dw = int(dw_m.group(1)) if dw_m else 1000
    return widths, dw


def _build_w_array_from_map(widths: Dict[int, int]) -> str:
    """Собрать /W [...] только для активных CID (компактный OpenPDF/Jasper)."""
    import tbank_unlock_template as tut
    return tut._build_widths_from_map(widths)


def find_object_range(pdf: bytes, xref: int) -> Optional[Tuple[int, int]]:
    """Байтовый диапазон объекта xref в сыром PDF (start, end)."""
    import tbank_unlock_template as tut

    try:
        offsets, _first, _count, _xref_off = tut._parse_xref_table(pdf)
    except RuntimeError:
        return None
    if xref not in offsets:
        return None
    start = offsets[xref]
    end = tut._find_object_end(pdf, start)
    return start, end


def _read_widths_from_orig(doc: fitz.Document, cidfont_xref: int) -> Dict[int, int]:
    """Parse /W array from CIDFontType2 dict, return {cid: advance_width}."""
    obj = doc.xref_object(cidfont_xref)
    widths, _dw = _parse_w_array(obj)
    return widths


def _find_font_meta(doc: fitz.Document) -> Dict[str, dict]:
    """Find Type0 font dicts → return mapping basefont_short_name → meta."""
    fonts = {}
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref)
        except Exception:
            continue
        if "/Type /Font" not in obj or "/Subtype /Type0" not in obj:
            continue
        m_base = re.search(r"/BaseFont\s*/([^\s/<>]+)", obj)
        if not m_base:
            continue
        name = m_base.group(1)
        m_tu = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
        m_desc = re.search(r"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R\s*\]", obj)
        if not (m_tu and m_desc):
            continue
        cidfont_xref = int(m_desc.group(1))
        short = name.split("+")[-1]  # 'TinkoffSans-Regular'
        fonts[short] = {
            "type0_xref": xref,
            "cidfont_xref": cidfont_xref,
            "tounicode_xref": int(m_tu.group(1)),
        }
    return fonts


class OrigContext:
    """Контекст работы с оригинальным шаблоном без unlock'а.

    После .load(orig_pdf_path) предоставляет:
      .uni_to_cid_reg    — {unicode → CID} для Regular subset
      .uni_to_cid_med    — {unicode → CID} для Medium subset
      .width_reg, .width_med — {CID → advance_width в font units}
      .upem              — ед/em (для Tinkoff = 2048)
      .pdf_bytes         — байты PDF
      .cs_xref           — xref content stream
      .cs_cs, .cs_ce     — позиции тела stream'а в pdf_bytes
      .cs_raw            — сырые сжатые байты stream'а
      .cs_dec            — расжатый stream
    """

    def __init__(self):
        self.pdf_bytes: bytes = b""
        self.uni_to_cid_reg: Dict[int, int] = {}
        self.uni_to_cid_med: Dict[int, int] = {}
        self.width_reg: Dict[int, int] = {}
        self.width_med: Dict[int, int] = {}
        self.dw_reg: int = 1000
        self.dw_med: int = 1000
        self.tounicode_xref_reg: int = 0
        self.tounicode_xref_med: int = 0
        self.cidfont_xref_reg: int = 0
        self.cidfont_xref_med: int = 0
        self.fontfile_xref_reg: int = 0
        self.fontfile_xref_med: int = 0
        self.upem: int = 1000
        self.cs_xref: int = 0
        self.cs_cs: int = 0
        self.cs_ce: int = 0
        self.cs_raw: bytes = b""
        self.cs_dec: bytes = b""

    def load(self, orig_pdf_path: str) -> bool:
        try:
            with open(orig_pdf_path, "rb") as f:
                self.pdf_bytes = f.read()

            doc = fitz.open(orig_pdf_path)
            import tbank_unlock_template as tut
            fonts = _find_font_meta(doc)
            fm = tut._find_font_objects(doc)

            f_reg = None
            f_med = None
            for name, meta in fonts.items():
                if "Regular" in name:
                    f_reg = meta
                elif "Medium" in name:
                    f_med = meta
            if not (f_reg and f_med):
                logger.warning("orig mode: Tinkoff Regular/Medium not found")
                doc.close()
                return False

            sub_to_uni_reg = _parse_subset_tounicode(
                doc.xref_stream(f_reg["tounicode_xref"]).decode("latin1", errors="replace")
            )
            sub_to_uni_med = _parse_subset_tounicode(
                doc.xref_stream(f_med["tounicode_xref"]).decode("latin1", errors="replace")
            )
            # Inverse: unicode → CID
            self.uni_to_cid_reg = {u: c for c, u in sub_to_uni_reg.items()}
            self.uni_to_cid_med = {u: c for c, u in sub_to_uni_med.items()}

            # Widths from /W array; identity CID==GID for Type0/CIDFontType2
            self.width_reg = _read_widths_from_orig(doc, f_reg["cidfont_xref"])
            self.width_med = _read_widths_from_orig(doc, f_med["cidfont_xref"])
            _, self.dw_reg = _parse_w_array(doc.xref_object(f_reg["cidfont_xref"]))
            _, self.dw_med = _parse_w_array(doc.xref_object(f_med["cidfont_xref"]))
            self.tounicode_xref_reg = f_reg["tounicode_xref"]
            self.tounicode_xref_med = f_med["tounicode_xref"]
            self.cidfont_xref_reg = f_reg["cidfont_xref"]
            self.cidfont_xref_med = f_med["cidfont_xref"]
            fr = fm.get("TinkoffSans-Regular") or {}
            fm_m = fm.get("TinkoffSans-Medium") or {}
            self.fontfile_xref_reg = fr.get("fontfile_xref", 0)
            self.fontfile_xref_med = fm_m.get("fontfile_xref", 0)

            # Detect unitsPerEm: TinkoffSans = 2048
            self.upem = 2048

            # Locate page content stream
            page = doc[0]
            cs_xref = page.get_contents()[0]
            self.cs_xref = cs_xref
            self.cs_raw = doc.xref_stream(cs_xref, file=False) if False else doc.xref_stream(cs_xref)
            # ↑ doc.xref_stream returns DECOMPRESSED already
            # We need to also locate stream byte offsets in pdf_bytes:
            stream_meta = _find_stream_pos_for_xref(self.pdf_bytes, cs_xref)
            if stream_meta is None:
                logger.warning("orig mode: cannot locate content stream bytes")
                doc.close()
                return False
            cs, ce = stream_meta
            self.cs_cs = cs
            self.cs_ce = ce
            self.cs_dec = doc.xref_stream(cs_xref)
            # Save the actual compressed stream bytes from file
            self.cs_raw = self.pdf_bytes[cs:ce]
            doc.close()
            return True
        except Exception as e:
            logger.warning(f"orig mode load failed: {e}")
            return False

    def can_render_reg(self, *texts: str) -> Tuple[bool, List[str]]:
        """Check whether all chars in `texts` are present in regular orig subset.
        Returns (ok, list_of_missing_chars)."""
        missing = []
        for t in texts:
            for ch in t:
                if ord(ch) not in self.uni_to_cid_reg and ch not in missing:
                    missing.append(ch)
        return (not missing, missing)

    def can_render_med(self, *texts: str) -> Tuple[bool, List[str]]:
        missing = []
        for t in texts:
            for ch in t:
                if ord(ch) not in self.uni_to_cid_med and ch not in missing:
                    missing.append(ch)
        return (not missing, missing)

    def enc(self, text: str, medium: bool = False) -> bytes:
        """Encode text to PDF string body using orig subset CIDs."""
        table = self.uni_to_cid_med if medium else self.uni_to_cid_reg
        out = bytearray()
        for ch in text:
            if ch in (" ", "\u00a0"):
                cid = 3
            else:
                cid = table.get(ord(ch))
            if cid is None:
                return b""  # caller must check can_render first
            for byte in ((cid >> 8) & 0xFF, cid & 0xFF):
                out.extend(_ESC.get(byte, bytes([byte])))
        return bytes(out)

    def text_width(self, text: str, font_size: float, medium: bool = False) -> float:
        """Advance width in PDF user units (/W + /DW from donor CIDFont)."""
        table_w = self.width_med if medium else self.width_reg
        table_c = self.uni_to_cid_med if medium else self.uni_to_cid_reg
        dw = self.dw_med if medium else self.dw_reg
        total = 0
        for ch in text:
            if ch in (" ", "\u00a0"):
                cid = 3
            else:
                cid = table_c.get(ord(ch))
            if cid is None:
                return 0.0
            total += table_w.get(cid, dw)
        return total * font_size / 1000.0


def fit_amount_to_enc_len(
    ctx: OrigContext,
    orig_amt: str,
    new_amt: str,
    medium: bool = False,
) -> Tuple[str, Optional[bytes]]:
    """Подгонка суммы под CID-слот: ровно ОДИН пробел перед F3 ₽.

    Никогда не добиваем хвостовыми пробелами («100    ₽»).
    Недостающую длину слота — только octal-grow escapes.
    Если байты не сходятся → None (caller сдвигает Tm / dynamic path).
    """
    enc = lambda t: ctx.enc(t, medium=medium)
    old_b = enc(orig_amt)
    if not old_b:
        return new_amt, None
    core = new_amt.strip()
    base = core + " "
    b = enc(base)
    if not b:
        return new_amt, None
    if len(b) == len(old_b):
        return base, b
    if len(b) < len(old_b):
        try:
            from sber_dynamic import _grow_pdf_literal
        except Exception:
            _grow_pdf_literal = None  # type: ignore
        if _grow_pdf_literal is not None:
            grown = _grow_pdf_literal(b, len(old_b))
            if grown is not None and len(grown) == len(old_b):
                return base, grown
    # Do not fall back to multi-space / lead-pad fits — those leave a hole before ₽
    # or break right-edge alignment. Caller must Tm-realign with natural length.
    return base, None


def _enc_pad_chars(ctx: "OrigContext", medium: bool) -> List[Tuple[str, int]]:
    """Только пробел как pad. Буквы (а/я/б/ы…) → видимый мусор в ФИО (Рыбаковя)
    и OnlyPDF FAKE. Длину слота не добиваем буквами — caller делает Tm-realign.
    """
    table = ctx.uni_to_cid_med if medium else ctx.uni_to_cid_reg
    if ord(" ") not in table:
        return []
    esc = ctx.enc(" ", medium=medium)
    if not esc:
        return []
    return [(" ", len(esc))]


def _grow_enc_to_len(
    ctx: "OrigContext",
    base: str,
    target_len: int,
    medium: bool,
) -> Tuple[str, Optional[bytes]]:
    """Space-pad grow disabled — Proton TBANK_*_WHITESPACE HARD on face.

    Return exact match only; unequal lengths → caller Tm-realign or keep donor.
    """
    enc = lambda t: ctx.enc(t, medium=medium)
    b0 = enc(base)
    if b0 and len(b0) == target_len and base == base.strip():
        return base, b0
    return base, None


def _mutate_to_enc_len(
    ctx: "OrigContext",
    text: str,
    target_len: int,
    medium: bool,
    *,
    allow_trim: bool,
) -> Tuple[str, Optional[bytes]]:
    """Подгонка длины без подмены букв.

    Раньше сюда совали «а»/escape-глифы — на чеке вылезало «Рыбаковя», «Павловб».
    Только trim (если разрешён) + пробельный grow; иначе None → Tm-realign.
    """
    enc = lambda t: ctx.enc(t, medium=medium)
    bases = [text]
    if allow_trim:
        cur = text
        while cur:
            b = enc(cur)
            if b and len(b) <= target_len:
                bases.append(cur)
                break
            cur = cur[:-1].rstrip()
            if cur and cur not in bases:
                bases.append(cur)

    for base in bases:
        b = enc(base)
        if not b:
            continue
        if len(b) == target_len:
            return base, b
        if len(b) < target_len:
            grown, gb = _grow_enc_to_len(ctx, base, target_len, medium)
            if gb is not None:
                return grown, gb
    return text, None


def fit_text_to_enc_len(
    ctx: OrigContext, text: str, target_len: int, medium: bool = False,
    *, allow_trim: bool = True,
) -> Tuple[str, Optional[bytes]]:
    """Подгонка текста под длину CID-слота донора (без сдвига Tm)."""
    enc = lambda t: ctx.enc(t, medium=medium)
    if not text:
        return text, None

    candidates: List[str] = [text]
    m = re.match(r"(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}):\d{2}", text.strip())
    if m:
        candidates.append(m.group(1))
        candidates.append(m.group(1) + " ")
    stripped = text.strip()
    if stripped != text:
        candidates.append(stripped)

    seen: set = set()
    ordered: List[str] = []
    for cand in candidates:
        if cand not in seen:
            seen.add(cand)
            ordered.append(cand)

    for cand in ordered:
        b = enc(cand)
        if not b:
            continue
        if len(b) == target_len:
            # Reject trailing/leading ASCII spaces on non-amount face text —
            # Proton TBANK_*_WHITESPACE HARD.
            if cand != cand.strip() and not any(ch.isdigit() for ch in cand):
                continue
            return cand, b
        # No ASCII-space grow for FIO/labels (WHITESPACE HARD). Amounts use
        # fit_amount_to_enc_len separately. Trim-only for oversize slots.
        if allow_trim and len(b) > target_len:
            for trim in range(1, len(cand) + 1):
                short = cand[: len(cand) - trim].rstrip()
                if not short or short != short.strip():
                    continue
                b = enc(short)
                if b and len(b) == target_len:
                    return short, b
        mutated, mb = _mutate_to_enc_len(
            ctx, cand, target_len, medium, allow_trim=allow_trim,
        )
        if mb is not None and mutated == mutated.strip():
            return mutated, mb
    return text, None


def find_tj_inner_by_len(stream: bytes, target_len: int) -> Optional[bytes]:
    """Найти тело (...)Tj с заданной длиной (байт CID-слота)."""
    for m in re.finditer(rb"\(([^()]*(?:\\.[^()]*)*)\)Tj", stream):
        inner = m.group(1)
        if len(inner) == target_len:
            return inner
    return None


def replace_tj_bytes_inplace(
    stream: bytes,
    old_b: bytes,
    new_b: bytes,
    occurrence: int = 1,
) -> Tuple[bytes, bool]:
    """Замена только (...)Tj — координаты Tm не трогаем (skeleton корпуса)."""
    if old_b == new_b:
        return stream, True
    if len(old_b) != len(new_b):
        return stream, False
    needle = b"(" + old_b + b")Tj"
    start = 0
    pos = -1
    for _ in range(occurrence):
        pos = stream.find(needle, start)
        if pos < 0:
            return stream, False
        start = pos + len(needle)
    return stream[:pos] + b"(" + new_b + b")Tj" + stream[pos + len(needle):], True


def replace_field_preserve_tm(
    ctx: OrigContext,
    stream: bytes,
    old_text: str,
    new_text: str,
    *,
    medium: bool = False,
    occurrence: int = 1,
    allow_trim: bool = True,
) -> Tuple[bytes, str, bool]:
    """In-place поле: тот же Tm, тот же размер CID-слота."""
    enc = lambda t: ctx.enc(t, medium=medium)
    old_b = enc(old_text)
    if not old_b:
        return stream, new_text, False
    fitted, new_b = fit_text_to_enc_len(
        ctx, new_text, len(old_b), medium=medium, allow_trim=allow_trim,
    )
    if new_b is None:
        return stream, new_text, False
    stream, ok = replace_tj_bytes_inplace(stream, old_b, new_b, occurrence=occurrence)
    return stream, fitted, ok


def replace_amount_preserve_tm(
    ctx: OrigContext,
    stream: bytes,
    old_text: str,
    new_text: str,
    *,
    medium: bool = False,
    occurrence: int = 1,
    font_size: Optional[float] = None,
) -> Tuple[bytes, str, bool]:
    """Сумма: 1 пробел перед ₽ + сдвиг Tm (правый край как у донора).

    preserve_tm без сдвига при короткой сумме даёт «100    ₽».
    Байты Tj — equal-length (octal-grow); X в Tm — same-token-len когда можно.
    """
    enc = lambda t: ctx.enc(t, medium=medium)
    old_b = enc(old_text)
    if not old_b:
        return stream, new_text, False
    fitted, new_b = fit_amount_to_enc_len(ctx, old_text, new_text, medium=medium)
    if new_b is None:
        # Natural-length encode — stream length may change (dynamic / non-flate).
        fitted = new_text.strip() + " "
        new_b = enc(fitted)
        if not new_b:
            return stream, new_text, False
    needle = b"(" + old_b + b")Tj"
    start = 0
    pos = -1
    for _ in range(max(1, occurrence)):
        pos = stream.find(needle, start)
        if pos < 0:
            return stream, fitted, False
        start = pos + len(needle)
    look_from = max(0, pos - 200)
    region = stream[look_from:pos]
    tms = list(re.finditer(rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm", region))
    new_tj = b"(" + new_b + b")Tj"
    if not tms:
        stream2 = stream[:pos] + new_tj + stream[pos + len(needle):]
        return stream2, fitted, True
    last = tms[-1]
    old_x = float(last.group(1))
    x_tok = last.group(1)
    if font_size is None:
        tf = list(re.finditer(rb"/F[123]\s+([\d.]+)\s+Tf", region))
        font_size = float(tf[-1].group(1)) if tf else (16.0 if medium else 9.0)
    old_w = ctx.text_width(old_text, font_size, medium=medium)
    new_w = ctx.text_width(fitted, font_size, medium=medium)
    new_x = old_x + (old_w - new_w)
    try:
        from tbank_sbp_stealth import _fmt_coord_match, _fmt_coord
        x_new = _fmt_coord_match(new_x, x_tok).encode("ascii")
        if len(x_new) != len(x_tok):
            x_new = _fmt_coord(new_x).encode("ascii")
    except Exception:
        x_new = f"{new_x:.2f}".rstrip("0").rstrip(".").encode("ascii")
    abs_x = look_from + last.start(1)
    stream2 = (
        stream[:abs_x] + x_new + stream[abs_x + len(x_tok):pos] + new_tj
        + stream[pos + len(needle):]
    )
    return stream2, fitted, True


def _collect_used_cids(stream: bytes) -> Dict[str, Set[int]]:
    """CIDs по /F1 /F2 /F3 (F3 → Regular) с учётом смены шрифта внутри BT-блока."""
    used: Dict[str, Set[int]] = {"F1": set(), "F2": set()}

    def _add_cids(chunk: bytes, font_res: str) -> None:
        target = "F2" if font_res == "F2" else "F1"
        for tj in re.finditer(rb"\(([^()]*(?:\\.[^()]*)*)\)Tj", chunk):
            body = _decode_pdf_string(tj.group(1))
            for i in range(0, len(body) - 1, 2):
                used[target].add((body[i] << 8) | body[i + 1])

    for m in re.finditer(rb"BT\s*(.*?)\s*ET", stream, re.DOTALL):
        block = m.group(1)
        chunks = re.split(rb"/(F[123])\s+[\d.]+\s+Tf", block)
        cur = "F1"
        for i, chunk in enumerate(chunks):
            if i == 0:
                if chunk:
                    _add_cids(chunk, cur)
                continue
            if i % 2 == 1:
                cur = chunk.decode("ascii", "replace")
            else:
                _add_cids(chunk, cur)
    return used


def _find_stream_pos_for_xref(pdf: bytes, xref: int) -> Optional[Tuple[int, int]]:
    """Find (cs, ce) — content bytes positions of compressed stream for given object xref."""
    # Find object header `<xref> 0 obj`
    needle = f"\n{xref} 0 obj".encode()
    pos = pdf.find(needle)
    if pos < 0:
        # Try beginning of file (no leading \n)
        needle2 = f"{xref} 0 obj".encode()
        pos = pdf.find(needle2)
        if pos < 0:
            return None
    # Find 'stream' keyword
    s = pdf.find(b"stream", pos)
    if s < 0:
        return None
    cs = s + 6
    if pdf[cs:cs+2] == b"\r\n":
        cs += 2
    elif pdf[cs:cs+1] == b"\n":
        cs += 1
    es = pdf.find(b"endstream", cs)
    if es < 0:
        return None
    ce = es
    if pdf[ce-2:ce] == b"\r\n":
        ce -= 2
    elif pdf[ce-1:ce] == b"\n":
        ce -= 1
    return (cs, ce)
