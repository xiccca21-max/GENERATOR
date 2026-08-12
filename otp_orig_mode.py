"""
OTP ORIG MODE — режим работы с оригинальным шаблоном ОТП Банка
без замены шрифтов. Аналогично tbank_orig_mode, но для шрифтов
Onest-Regular / Onest-SemiBold / Podkova-Bold.
"""

import re
import zlib
import logging
from typing import Dict, Optional, Tuple, List

import fitz

logger = logging.getLogger(__name__)

_ESC = {
    0x28: b"\\(", 0x29: b"\\)", 0x5C: b"\\\\",
    0x0A: b"\\n", 0x0D: b"\\r",
    0x08: b"\\b", 0x0C: b"\\f", 0x09: b"\\t",
}


def _parse_subset_tounicode(stream_text: str) -> Dict[int, int]:
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


def _extract_balanced_w_body(obj: str) -> str:
    """Найти содержимое внешнего `/W [ ... ]` с учётом вложенных скобок."""
    idx = obj.find("/W")
    if idx < 0:
        return ""
    i = obj.find("[", idx)
    if i < 0:
        return ""
    depth = 0
    start = i
    for j in range(i, len(obj)):
        c = obj[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return obj[start + 1:j]
    return ""


def _read_widths_from_orig(doc: fitz.Document, cidfont_xref: int) -> Dict[int, int]:
    """Parse /W array from CIDFontType2 dict, return {gid: advance_width}.

    Поддерживает оба формата PDF spec:
      <gid> [ w1 w2 ... ]   — последовательно начиная с gid
      <gid_lo> <gid_hi> w   — диапазон gid_lo..gid_hi с одинаковой шириной w
    """
    obj = doc.xref_object(cidfont_xref)
    body = _extract_balanced_w_body(obj)
    if not body:
        return {}
    widths: Dict[int, int] = {}
    tokens = re.findall(r"\d+|\[|\]", body)
    j = 0
    while j < len(tokens):
        if tokens[j] in ("[", "]"):
            j += 1
            continue
        gid = int(tokens[j]); j += 1
        if j < len(tokens) and tokens[j] == "[":
            j += 1
            run = []
            while j < len(tokens) and tokens[j] != "]":
                run.append(int(tokens[j])); j += 1
            if j < len(tokens) and tokens[j] == "]":
                j += 1
            for k, w in enumerate(run):
                widths[gid + k] = w
        else:
            if j + 1 < len(tokens):
                gid_hi = int(tokens[j]); w = int(tokens[j + 1]); j += 2
                for g in range(gid, gid_hi + 1):
                    widths[g] = w
    return widths


def _find_font_meta(doc: fitz.Document) -> Dict[str, dict]:
    """Find Type0 font dicts → mapping basefont_short_name → meta."""
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
        short = name.split("+")[-1]   # 'Onest-SemiBold'
        fonts[short] = {
            "type0_xref": xref,
            "cidfont_xref": cidfont_xref,
            "tounicode_xref": int(m_tu.group(1)),
        }
    return fonts


class OtpOrigContext:
    """Контекст работы с оригинальным шаблоном ОТП.

    После .load() предоставляет:
      .uni_to_cid_reg  — Onest-Regular
      .uni_to_cid_sb   — Onest-SemiBold
      .uni_to_cid_pod  — Podkova-Bold
      .width_*         — словари ширин
      .upem            — ед/em
      .cs_xref / .cs_cs / .cs_ce / .cs_raw / .cs_dec
    """

    def __init__(self):
        self.pdf_bytes: bytes = b""
        self.uni_to_cid_reg: Dict[int, int] = {}
        self.uni_to_cid_sb: Dict[int, int] = {}
        self.uni_to_cid_pod: Dict[int, int] = {}
        self.width_reg: Dict[int, int] = {}
        self.width_sb: Dict[int, int] = {}
        self.width_pod: Dict[int, int] = {}
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
            fonts = _find_font_meta(doc)

            f_reg = f_sb = f_pod = None
            # Сначала ищем шрифты с subset-prefix (orig "XXXXXX+Name"),
            # игнорируя добавленные нами F4/F5 (которые без "+").
            for name, meta in fonts.items():
                if "+" not in name:
                    continue
                if "Onest-Regular" in name:
                    f_reg = meta
                elif "Onest-SemiBold" in name:
                    f_sb = meta
                elif "Podkova" in name:
                    f_pod = meta
            # Fallback: если нет шрифтов с prefix (например, generated unlocked
            # с другими именами) — берём любые подходящие.
            if not (f_reg and f_sb):
                for name, meta in fonts.items():
                    if not f_reg and "Onest-Regular" in name:
                        f_reg = meta
                    elif not f_sb and "Onest-SemiBold" in name:
                        f_sb = meta
                    elif not f_pod and "Podkova" in name:
                        f_pod = meta
            if not (f_reg and f_sb):
                logger.warning("OTP orig mode: Onest fonts not found")
                doc.close()
                return False

            sub_to_uni_reg = _parse_subset_tounicode(
                doc.xref_stream(f_reg["tounicode_xref"]).decode("latin1", errors="replace")
            )
            sub_to_uni_sb = _parse_subset_tounicode(
                doc.xref_stream(f_sb["tounicode_xref"]).decode("latin1", errors="replace")
            )
            sub_to_uni_pod = {}
            if f_pod:
                sub_to_uni_pod = _parse_subset_tounicode(
                    doc.xref_stream(f_pod["tounicode_xref"]).decode("latin1", errors="replace")
                )

            self.uni_to_cid_reg = {u: c for c, u in sub_to_uni_reg.items()}
            self.uni_to_cid_sb  = {u: c for c, u in sub_to_uni_sb.items()}
            self.uni_to_cid_pod = {u: c for c, u in sub_to_uni_pod.items()}

            self.width_reg = _read_widths_from_orig(doc, f_reg["cidfont_xref"])
            self.width_sb  = _read_widths_from_orig(doc, f_sb["cidfont_xref"])
            if f_pod:
                self.width_pod = _read_widths_from_orig(doc, f_pod["cidfont_xref"])

            self.upem = 1000   # Onest/Podkova: upem=1000

            page = doc[0]
            cs_xref = page.get_contents()[0]
            self.cs_xref = cs_xref
            self.cs_dec = doc.xref_stream(cs_xref)
            stream_meta = _find_stream_pos_for_xref(self.pdf_bytes, cs_xref)
            if stream_meta is None:
                doc.close()
                return False
            cs, ce = stream_meta
            self.cs_cs = cs
            self.cs_ce = ce
            self.cs_raw = self.pdf_bytes[cs:ce]
            doc.close()
            return True
        except Exception as e:
            logger.warning(f"OTP orig mode load failed: {e}")
            return False

    # ── Универсальный API: medium=False → Regular, medium=True → SemiBold ──
    def can_render_reg(self, *texts: str) -> Tuple[bool, List[str]]:
        miss = []
        for t in texts:
            for ch in t:
                if ord(ch) not in self.uni_to_cid_reg and ch not in miss:
                    miss.append(ch)
        return (not miss, miss)

    def can_render_sb(self, *texts: str) -> Tuple[bool, List[str]]:
        miss = []
        for t in texts:
            for ch in t:
                if ord(ch) not in self.uni_to_cid_sb and ch not in miss:
                    miss.append(ch)
        return (not miss, miss)

    def enc(self, text: str, semibold: bool = False) -> bytes:
        """Encode → 2-byte CIDs, escaped."""
        table = self.uni_to_cid_sb if semibold else self.uni_to_cid_reg
        out = bytearray()
        for ch in text:
            cid = table.get(ord(ch))
            if cid is None:
                return b""
            for byte in ((cid >> 8) & 0xFF, cid & 0xFF):
                out.extend(_ESC.get(byte, bytes([byte])))
        return bytes(out)

    def text_width(self, text: str, font_size: float, semibold: bool = False) -> float:
        table_w = self.width_sb if semibold else self.width_reg
        table_c = self.uni_to_cid_sb if semibold else self.uni_to_cid_reg
        total = 0
        for ch in text:
            cid = table_c.get(ord(ch))
            if cid is None:
                return 0.0
            total += table_w.get(cid, 500)
        return total * font_size / 1000.0


def _find_stream_pos_for_xref(pdf: bytes, xref: int) -> Optional[Tuple[int, int]]:
    needle = f"\n{xref} 0 obj".encode()
    pos = pdf.find(needle)
    if pos < 0:
        needle2 = f"{xref} 0 obj".encode()
        pos = pdf.find(needle2)
        if pos < 0:
            return None
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


def encode_hex(stream_bytes: bytes) -> bytes:
    """OTP использует hex-формат строк: <abcdef...>Tj вместо (\\xhh\\xhh)Tj."""
    return stream_bytes.hex().encode("ascii")
