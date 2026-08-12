"""
OTP CARD ORIG MODE — контекст для работы с шаблоном `OTP_card_original.pdf`.

Особенности шаблона (mPDF 8.3.1):
  • F2 — Onest-Regular (12pt grey, 12/14pt black)
  • F3 — Onest-SemiBold (20pt header, 24pt total amount)
  • F4 — Podkova-Medium (12pt — внутри синей рамки «АО ОТП БАНК / БИК»)
  • F5 — Helvetica-Regular (9pt — нижний bytecode-стрип в рамке)

Кодировка строк: **Identity-H** где **CID == GID == Unicode codepoint**
(`/ToUnicode` — bfrange `<0000> <FFFF> <0000>`). Это значит что в content stream
строки записаны как 2-байтовые big-endian Unicode (utf-16be) внутри `(…)Tj`.

Чтобы определить какие глифы реально доступны в subset, мы парсим встроенный
TrueType FontFile2 через fontTools (`cmap` + `hmtx`).
"""

import re
import logging
from typing import Dict, List, Optional, Tuple

import fitz

logger = logging.getLogger(__name__)


_ESC = {
    0x28: b"\\(", 0x29: b"\\)", 0x5C: b"\\\\",
    0x0A: b"\\n", 0x0D: b"\\r",
    0x08: b"\\b", 0x0C: b"\\f", 0x09: b"\\t",
}


def _find_stream_pos(pdf: bytes, xref: int) -> Optional[Tuple[int, int]]:
    needle = f"\n{xref} 0 obj".encode()
    pos = pdf.find(needle)
    if pos < 0:
        needle = f"{xref} 0 obj".encode()
        pos = pdf.find(needle)
        if pos < 0:
            return None
    s = pdf.find(b"stream", pos)
    if s < 0:
        return None
    cs = s + 6
    if pdf[cs:cs + 2] == b"\r\n":
        cs += 2
    elif pdf[cs:cs + 1] == b"\n":
        cs += 1
    es = pdf.find(b"endstream", cs)
    if es < 0:
        return None
    ce = es
    if pdf[ce - 2:ce] == b"\r\n":
        ce -= 2
    elif pdf[ce - 1:ce] == b"\n":
        ce -= 1
    return (cs, ce)


def _extract_balanced_w(obj_text: str) -> str:
    idx = obj_text.find("/W")
    if idx < 0:
        return ""
    i = obj_text.find("[", idx)
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(obj_text)):
        c = obj_text[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return obj_text[i + 1:j]
    return ""


def _parse_widths(obj_text: str) -> Dict[int, int]:
    """Парсит `/W` CIDFontType2 → {gid: advance}.

    В mPDF Identity-H GID == Unicode codepoint, поэтому ключи можно
    интерпретировать одновременно как «доступные unicode codepoints».
    Поддерживает форматы:
      <lo> <hi> w        — диапазон одинаковой ширины
      <gid> [ w1 w2 .. ] — массив начиная с gid
    """
    body = _extract_balanced_w(obj_text)
    if not body:
        return {}
    widths: Dict[int, int] = {}
    tokens = re.findall(r"\-?\d+\.?\d*|\[|\]", body)
    j = 0
    while j < len(tokens):
        if tokens[j] in ("[", "]"):
            j += 1
            continue
        try:
            gid = int(tokens[j])
        except ValueError:
            j += 1
            continue
        j += 1
        if j < len(tokens) and tokens[j] == "[":
            j += 1
            run = []
            while j < len(tokens) and tokens[j] != "]":
                try:
                    run.append(int(float(tokens[j])))
                except ValueError:
                    pass
                j += 1
            if j < len(tokens) and tokens[j] == "]":
                j += 1
            for k, w in enumerate(run):
                widths[gid + k] = w
        elif j + 1 < len(tokens):
            try:
                gid_hi = int(tokens[j])
                w = int(float(tokens[j + 1]))
                j += 2
                for g in range(gid, gid_hi + 1):
                    widths[g] = w
            except ValueError:
                j += 1
    return widths


def _parse_default_width(obj_text: str) -> int:
    m = re.search(r"/DW\s+(\-?\d+)", obj_text)
    return int(m.group(1)) if m else 540


def _parse_upem(fd_text: str) -> int:
    # /FontBBox [-171 -304 2855 960] → upem ≈ max — но проще взять fixed 1000
    return 1000


def _find_fonts(doc: fitz.Document) -> Dict[str, dict]:
    """Возвращает {tag: {type0_xref, cidfont_xref, font_label, fontfile_xref}}.

    tag = `F2`/`F3`/`F4`/`F5` — то, что используется в content stream.
    """
    # Сначала найти page Resources → /Font
    page = doc[0]
    page_xref = page.xref
    obj = doc.xref_object(page_xref)
    m_res = re.search(r"/Resources\s+(\d+)\s+0\s+R", obj)
    if m_res:
        res_obj = doc.xref_object(int(m_res.group(1)))
    else:
        m_res2 = re.search(r"/Resources\s*<<(.*?)>>", obj, re.S)
        res_obj = m_res2.group(0) if m_res2 else obj
    m_fdict = re.search(r"/Font\s*<<\s*(.*?)>>", res_obj, re.S)
    if not m_fdict:
        m_fr = re.search(r"/Font\s+(\d+)\s+0\s+R", res_obj)
        if m_fr:
            fd_obj = doc.xref_object(int(m_fr.group(1)))
            m_fdict = re.search(r"<<\s*(.*?)>>", fd_obj, re.S)
    if not m_fdict:
        return {}

    tag_to_xref: Dict[str, int] = {}
    for m in re.finditer(r"/(F\d+)\s+(\d+)\s+0\s+R", m_fdict.group(1)):
        tag_to_xref[m.group(1)] = int(m.group(2))

    fonts: Dict[str, dict] = {}
    for tag, xref in tag_to_xref.items():
        font_obj = doc.xref_object(xref)
        m_base = re.search(r"/BaseFont\s*/([^\s/<>]+)", font_obj)
        font_label = m_base.group(1).split("+")[-1] if m_base else ""

        # Для Type0 → берём /W из DescendantFonts. Для TrueType (Helvetica) — игнор.
        cidfont_xref = None
        m_dsc = re.search(r"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R", font_obj)
        if m_dsc:
            cidfont_xref = int(m_dsc.group(1))
        if cidfont_xref is None:
            continue

        cidfont_obj = doc.xref_object(cidfont_xref)
        widths = _parse_widths(cidfont_obj)
        dw = _parse_default_width(cidfont_obj)

        fonts[tag] = {
            "type0_xref":   xref,
            "cidfont_xref": cidfont_xref,
            "font_label":   font_label,
            "widths":       widths,
            "default_width": dw,
        }
    return fonts


class OtpCardOrigContext:
    """После .load() предоставляет:
      .pdf_bytes
      .fonts: {tag → {label, supported: set[int], width: {cp: int}, upem: int}}
      .cs_xref / .cs_cs / .cs_ce / .cs_raw / .cs_dec
    """

    def __init__(self):
        self.pdf_bytes: bytes = b""
        self.fonts: Dict[str, dict] = {}
        self.cs_xref: int = 0
        self.cs_cs: int = 0
        self.cs_ce: int = 0
        self.cs_raw: bytes = b""
        self.cs_dec: bytes = b""

    def load(self, pdf_path: str) -> bool:
        try:
            with open(pdf_path, "rb") as f:
                self.pdf_bytes = f.read()
            doc = fitz.open(pdf_path)

            font_meta = _find_fonts(doc)
            for tag, meta in font_meta.items():
                # Identity-H: GID == Unicode codepoint. Поддержанные codepoints =
                # ключи /W массива.
                widths = meta["widths"]
                self.fonts[tag] = {
                    "label":         meta["font_label"],
                    "supported":     set(widths.keys()),
                    "width":         widths,
                    "default_width": meta["default_width"],
                    "upem":          1000,
                }

            page = doc[0]
            self.cs_xref = page.get_contents()[0]
            self.cs_dec = doc.xref_stream(self.cs_xref)
            sm = _find_stream_pos(self.pdf_bytes, self.cs_xref)
            if sm is None:
                doc.close()
                return False
            self.cs_cs, self.cs_ce = sm
            self.cs_raw = self.pdf_bytes[self.cs_cs:self.cs_ce]
            doc.close()
            return True
        except Exception as e:
            logger.warning(f"OTP card orig load failed: {e}", exc_info=True)
            return False

    # ── API ─────────────────────────────────────────────────────────────────
    def can_render(self, tag: str, *texts: str) -> Tuple[bool, List[str]]:
        meta = self.fonts.get(tag)
        if not meta:
            return False, []
        supp = meta["supported"]
        miss: List[str] = []
        for t in texts:
            for ch in t:
                if ord(ch) not in supp and ch not in miss:
                    miss.append(ch)
        return (not miss, miss)

    def encode_string(self, text: str, tag: str) -> Optional[bytes]:
        """Кодирует text → байты для PDF-литерала (utf-16be + escape `()\\`)."""
        meta = self.fonts.get(tag)
        if not meta:
            return None
        supp = meta["supported"]
        out = bytearray()
        for ch in text:
            cp = ord(ch)
            if cp not in supp:
                return None
            hi, lo = (cp >> 8) & 0xFF, cp & 0xFF
            out.extend(_ESC.get(hi, bytes([hi])))
            out.extend(_ESC.get(lo, bytes([lo])))
        return bytes(out)

    def text_width(self, text: str, font_size: float, tag: str) -> float:
        meta = self.fonts.get(tag)
        if not meta:
            return 0.0
        w = meta["width"]
        dw = meta["default_width"]
        upem = meta["upem"] or 1000
        total = 0
        for ch in text:
            total += w.get(ord(ch), dw)
        return total * font_size / upem
