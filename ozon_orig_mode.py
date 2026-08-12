"""
OZON ORIG MODE — контекст для работы с оригинальным шаблоном Ozon SBP.

Парсит:
  • F4 (Regular) и F5 (Bold) шрифты — Type0 → CIDFontType2 → /W
  • ToUnicode CMaps (cid → unicode)
  • Content stream (декодированный)

Hex-формат `<CIDH>Tj` (Identity-H, как у Skia/PDF от Chromium).
Каждый символ — отдельный Tj с advance-Td между ними:
    <0248> Tj
    9.9119568 0 Td <02B9> Tj
    7.2379761 0 Td <02C7> Tj
    ...
"""

import re
import logging
from typing import Dict, List, Optional, Tuple

import fitz

logger = logging.getLogger(__name__)


# ─── Парсинг ToUnicode CMap ─────────────────────────────────────────────────

def _parse_tounicode(text: str) -> Dict[int, int]:
    """Парсит CMap → {cid: unicode}. Поддерживает bfrange и bfchar."""
    result: Dict[int, int] = {}
    # bfrange сегменты
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for m in re.finditer(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk
        ):
            cid_lo = int(m.group(1), 16)
            cid_hi = int(m.group(2), 16)
            uni_lo = int(m.group(3), 16)
            for i in range(cid_hi - cid_lo + 1):
                result[cid_lo + i] = uni_lo + i
    # bfchar сегменты
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            cid = int(m.group(1), 16)
            uni = int(m.group(2), 16)
            result[cid] = uni
    return result


# ─── Парсинг /W массива CIDFontType2 ────────────────────────────────────────

def _extract_balanced_w(obj_text: str) -> str:
    """Достаёт содержимое /W [...]."""
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
    """Парсит /W в CIDFontType2 → {gid: advance_width}.
    Поддерживает форматы:
      <gid> [w1 w2 ...]   — сегмент
      <gid_lo> <gid_hi> w — диапазон с одной шириной
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


# ─── Поиск позиции stream-объекта в исходных байтах ─────────────────────────

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


# ─── Главный контекст ───────────────────────────────────────────────────────

class OzonOrigContext:
    """После .load() предоставляет:
      .pdf_bytes        — исходные байты PDF
      .uni_to_cid_reg   — F4 (GTEestiProDisplay-Regular)
      .uni_to_cid_bold  — F5 (GTEestiProDisplay-Bold)
      .width_reg/bold   — gid → advance (font units, upem=1000)
      .upem             — 1000
      .cs_xref / .cs_cs / .cs_ce / .cs_raw / .cs_dec
    """

    def __init__(self):
        self.pdf_bytes: bytes = b""
        self.uni_to_cid_reg: Dict[int, int] = {}
        self.uni_to_cid_bold: Dict[int, int] = {}
        self.width_reg: Dict[int, int] = {}
        self.width_bold: Dict[int, int] = {}
        self.upem: int = 1000
        self.cs_xref: int = 0
        self.cs_cs: int = 0
        self.cs_ce: int = 0
        self.cs_raw: bytes = b""
        self.cs_dec: bytes = b""
        # Pair kerning, наблюдаемый в content stream шаблона:
        # {(font_name "F4"|"F5", cid_left, cid_right): kern_units (1000-EM)}.
        # Skia/Chromium применяет GPOS/kern из ПОЛНОГО шрифта при рендере,
        # а в FontFile2 subset эти таблицы вырезаны — так что единственный
        # источник pair-кернинга для нас — собственно advances в orig stream.
        self.pair_kern: Dict[Tuple[str, int, int], float] = {}

    def load(self, path: str) -> bool:
        try:
            with open(path, "rb") as f:
                self.pdf_bytes = f.read()
            doc = fitz.open(path)

            # Найти Type0 шрифты по BaseFont
            f4_meta = f5_meta = None
            for xref in range(1, doc.xref_length()):
                try:
                    obj = doc.xref_object(xref)
                except Exception:
                    continue
                if "/Type /Font" not in obj or "/Subtype /Type0" not in obj:
                    continue
                m_base = re.search(r"/BaseFont\s*/([^\s/<>]+)", obj)
                m_tu   = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
                m_dsc  = re.search(r"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R\s*\]", obj)
                if not (m_base and m_tu and m_dsc):
                    continue
                name = m_base.group(1).split("+")[-1]  # "GTEestiProDisplay-Regular"
                meta = {
                    "type0_xref": xref,
                    "tounicode_xref": int(m_tu.group(1)),
                    "cidfont_xref":   int(m_dsc.group(1)),
                }
                if "Regular" in name and f4_meta is None:
                    f4_meta = meta
                elif "Bold" in name and f5_meta is None:
                    f5_meta = meta
            if not (f4_meta and f5_meta):
                logger.warning("Ozon orig: F4/F5 fonts not found")
                doc.close()
                return False

            # ToUnicode -> uni_to_cid
            cid_to_uni_reg = _parse_tounicode(
                doc.xref_stream(f4_meta["tounicode_xref"]).decode("latin1", "replace")
            )
            cid_to_uni_bold = _parse_tounicode(
                doc.xref_stream(f5_meta["tounicode_xref"]).decode("latin1", "replace")
            )
            self.uni_to_cid_reg  = {u: c for c, u in cid_to_uni_reg.items()}
            self.uni_to_cid_bold = {u: c for c, u in cid_to_uni_bold.items()}

            # /W array из CIDFontType2
            self.width_reg  = _parse_widths(doc.xref_object(f4_meta["cidfont_xref"]))
            self.width_bold = _parse_widths(doc.xref_object(f5_meta["cidfont_xref"]))

            # Content stream (page 0)
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

            self._extract_pair_kern()
            return True
        except Exception as e:
            logger.warning(f"Ozon orig load failed: {e}", exc_info=True)
            return False

    # ── API ─────────────────────────────────────────────────────────────────
    def can_render_reg(self, *texts: str) -> Tuple[bool, List[str]]:
        miss: List[str] = []
        for t in texts:
            for ch in t:
                if ord(ch) not in self.uni_to_cid_reg and ch not in miss:
                    miss.append(ch)
        return (not miss, miss)

    def can_render_bold(self, *texts: str) -> Tuple[bool, List[str]]:
        miss: List[str] = []
        for t in texts:
            for ch in t:
                if ord(ch) not in self.uni_to_cid_bold and ch not in miss:
                    miss.append(ch)
        return (not miss, miss)

    def cids(self, text: str, bold: bool = False) -> Optional[List[int]]:
        table = self.uni_to_cid_bold if bold else self.uni_to_cid_reg
        out = []
        for ch in text:
            cid = table.get(ord(ch))
            if cid is None:
                return None
            out.append(cid)
        return out

    def glyph_advances(self, cids: List[int], font_size: float, bold: bool = False) -> List[float]:
        """Advance каждого глифа В МЕЖГЛИФОВЫХ КООРДИНАТАХ (font_units/1000 * font_size).

        advances[i] = width(cids[i]) + kern(cids[i], cids[i+1]) — то есть
        смещение от точки draw glyph i к точке draw glyph i+1.

        Pair-kern берётся из self.pair_kern (наблюдения orig stream Skia).
        Если пары нет в таблице → kern=0 → чистая /W ширина.
        """
        table = self.width_bold if bold else self.width_reg
        font  = "F5" if bold else "F4"
        advs: List[float] = []
        n = len(cids)
        for i, cid in enumerate(cids):
            w = table.get(cid, 500)
            if i < n - 1:
                kern = self.pair_kern.get((font, cid, cids[i + 1]), 0.0)
                w += kern
            advs.append(w * font_size / self.upem)
        return advs

    def text_width(self, text: str, font_size: float, bold: bool = False) -> float:
        cids = self.cids(text, bold=bold)
        if cids is None:
            return 0.0
        # text_width = сумма advance + ширина последнего глифа без kern
        return sum(self.glyph_advances(cids, font_size, bold))

    # ── Извлечение pair-kern из content stream ──────────────────────────────
    def _extract_pair_kern(self) -> None:
        """Читает self.cs_dec, ищет последовательности
            /Fn fs Tf … Tm <C0> Tj <a0> 0 Td <C1> Tj <a1> 0 Td <C2> Tj …
        и для каждой пары (cid_i, cid_{i+1}) вычисляет наблюдаемый kern:
            kern_units = a_i * 1000 / fs - W[cid_i]
        Усредняет по всем наблюдениям."""
        if not self.cs_dec:
            return
        bt_segs = re.findall(rb"BT\b[\s\S]*?ET\b", self.cs_dec)
        font_pat  = re.compile(rb"/(F\d+)\s+(\-?\d+\.?\d*)\s+Tf")
        tj_pat    = re.compile(rb"<\s*([0-9A-Fa-f]+)\s*>\s*Tj")
        td_tj_pat = re.compile(rb"(\-?\d+\.?\d*)\s+0\s+Td\s*<\s*([0-9A-Fa-f]+)\s*>\s*Tj")

        accum: Dict[Tuple[str, int, int], List[float]] = {}
        for seg in bt_segs:
            fm = font_pat.search(seg)
            if not fm:
                continue
            font = fm.group(1).decode()
            fs   = float(fm.group(2))
            if fs <= 0:
                continue
            m0 = tj_pat.search(seg, fm.end())
            if not m0:
                continue
            cids = [int(m0.group(1), 16)]
            advs: List[float] = []
            pos = m0.end()
            while True:
                m = td_tj_pat.search(seg, pos)
                if not m:
                    break
                advs.append(float(m.group(1)))
                cids.append(int(m.group(2), 16))
                pos = m.end()
            if len(cids) < 2:
                continue
            table = self.width_bold if font == "F5" else self.width_reg
            for i, adv in enumerate(advs):
                cl, cr = cids[i], cids[i + 1]
                w_l = table.get(cl, 500)
                kern = adv * 1000.0 / fs - w_l
                accum.setdefault((font, cl, cr), []).append(kern)

        self.pair_kern = {k: sum(vs) / len(vs) for k, vs in accum.items()}
        logger.info(
            f"Ozon: extracted {len(self.pair_kern)} pair-kerns "
            f"({sum(1 for v in self.pair_kern.values() if abs(v) > 0.5)} non-zero)"
        )
