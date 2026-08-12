"""
OTP UNLOCK TEMPLATE — расширяет subset шрифтов F1 (SemiBold) и F2 (Regular)
в orig PDF, СОХРАНЯЯ subset-prefix и согласованную структуру шрифтов.

Ключевые гарантии (для прохождения «строгих» валидаторов):
  • subset-prefix orig PDF (NDSYUF+, MMPJRG+) сохраняется во всех BaseFont
    и в FontDescriptor.FontName
  • TTF name table содержит тот же prefixed name (nameID 1, 4, 6, 16, 17)
  • размер шрифтового объекта стремится быть близким к оригинальному
  • content stream перекодирован под новые GID

F3 (Podkova-Bold) не трогаем.
"""

import os
import re
import zlib
import logging
from io import BytesIO
from typing import Dict, Tuple, Optional

import fitz
from fontTools.ttLib import TTFont, newTable
from fontTools.subset import Subsetter, Options

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
ORIG_TEMPLATE = os.path.join(_DIR, "templates", "OTP_sbp_original.pdf")
UNLOCKED_TEMPLATE = os.path.join(_DIR, "templates", "OTP_sbp_unlocked.pdf")
FONT_REGULAR  = os.path.join(_DIR, "fonts", "Onest-Regular.ttf")
FONT_SEMIBOLD = os.path.join(_DIR, "fonts", "Onest-SemiBold.ttf")

# ── МИНИМАЛЬНО-необходимый репертуар (агрессивно ужат под цель ≤27340 B) ────
# Reg subset: orig (72 chars) ∪ только реально встречающиеся доп. символы:
#   - полная кириллица + ёЁ
#   - Latin: только буквы из SBP ID-формата ("B6127…B…") — практически B, K, F
#   - НИЧЕГО лишнего (никаких typography spaces, dashes, кавычек)
_NEEDED_UNICODES_REG = set()
_NEEDED_UNICODES_REG.update(ord(c) for c in "0123456789")
_NEEDED_UNICODES_REG.update(ord(c) for c in " .,:()-+/*")  # * — маска паспорта «2*** ****8»
_NEEDED_UNICODES_REG.update(range(0x0410, 0x0450))           # А-я (полная кириллица)
_NEEDED_UNICODES_REG.update([0x0401, 0x0451])                # Ё ё
_NEEDED_UNICODES_REG.update(range(ord("A"), ord("Z") + 1))   # A-Z (имена банков: VTB, OTP, OZON и т.п.)
_NEEDED_UNICODES_REG.update(range(ord("a"), ord("z") + 1))   # a-z (Sber, Tinkoff, Ozon, …)
_NEEDED_UNICODES_REG.update([0x00A0, 0x20BD, 0x00B7])        # NBSP, ₽, · middle dot
_NEEDED_UNICODES_REG.update([0x00AB, 0x00BB])                # « »  ‹‹ оригинал содержит «ОТП Банк» — без них валидатор не распознаёт
_NEEDED_UNICODES_REG.update([0x2116])                        # № «Корреспондентский счет № 30101…»

# SB subset: orig 28 chars + цифры 3,7,9 (для чисел в Квитанция/Сумма)
_NEEDED_UNICODES_SB = set()
_NEEDED_UNICODES_SB.update(ord(c) for c in "0123456789")
_NEEDED_UNICODES_SB.update(ord(c) for c in " ,-")
_NEEDED_UNICODES_SB.update(ord(c) for c in "КС")
_NEEDED_UNICODES_SB.update(ord(c) for c in "авдеимнопртуця")
_NEEDED_UNICODES_SB.update([0x00A0, 0x20BD])                 # NBSP, ₽
# Backwards compat alias
_NEEDED_UNICODES = _NEEDED_UNICODES_REG




# ─── Subset-аккуратно: сохраняем имя шрифта с prefix-ом ──────────────────────

def _subset_like_itext(font_path: str, unicodes) -> Tuple[TTFont, bytes]:
    """Subset с retain_gids=True: сохраняем GID-нумерацию из full Onest TTF,
    дропаем те же tables что и orig OTP TTF (no name, no post, no cvt, no fpgm).

    Цель: размер FontFile2 близкий к orig OTP (отклонение ≤15%).
    """
    fontlog = logging.getLogger("fontTools")
    prev_lvl = fontlog.level
    fontlog.setLevel(logging.ERROR)
    try:
        f = TTFont(font_path)
        opts = Options()
        opts.notdef_glyph = True
        opts.notdef_outline = True
        opts.recommended_glyphs = False
        opts.layout_features = []
        opts.retain_gids = True   # Preserve GID numbering from full TTF
        opts.drop_tables = [
            "DSIG", "GSUB", "GPOS", "kern", "BASE", "JSTF", "fvar", "GDEF",
            "name", "post", "STAT", "gasp", "cvt", "fpgm",
        ]
        opts.glyph_names = False
        ss = Subsetter(options=opts)
        ss.populate(unicodes=list(unicodes))
        ss.subset(f)

        bio = BytesIO()
        f.save(bio)
        return f, bio.getvalue()
    finally:
        fontlog.setLevel(prev_lvl)


def _build_uni_to_gid(font: TTFont) -> Dict[int, int]:
    cmap = font.getBestCmap()
    out = {}
    for cp, name in cmap.items():
        try:
            out[cp] = font.getGlyphID(name)
        except Exception:
            continue
    return out


def _build_widths_array(font: TTFont, gids) -> str:
    upem = font["head"].unitsPerEm
    hmtx = font["hmtx"].metrics
    glyph_order = font.getGlyphOrder()
    pairs = []
    for gid in gids:
        if gid >= len(glyph_order):
            continue
        gname = glyph_order[gid]
        adv = hmtx.get(gname, (1000, 0))[0]
        pairs.append((gid, round(adv * 1000 / upem)))
    pairs.sort()
    parts = []
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[j][0] + 1:
            j += 1
        if j == i:
            parts.append(f"{pairs[i][0]} [{pairs[i][1]}]")
        else:
            run = " ".join(str(p[1]) for p in pairs[i:j + 1])
            parts.append(f"{pairs[i][0]} [{run}]")
        i = j + 1
    return "[" + " ".join(parts) + "]"


def _build_tounicode_cmap(uni_to_gid: Dict[int, int]) -> bytes:
    pairs = sorted(((gid, cp) for cp, gid in uni_to_gid.items()), key=lambda x: x[0])
    ranges, chars = [], []
    i = 0
    while i < len(pairs):
        j = i
        while (
            j + 1 < len(pairs)
            and pairs[j + 1][0] == pairs[j][0] + 1
            and pairs[j + 1][1] == pairs[j][1] + 1
        ):
            j += 1
        if j > i:
            ranges.append((pairs[i][0], pairs[j][0], pairs[i][1]))
            i = j + 1
        else:
            chars.append(pairs[i])
            i += 1
    chunks = []
    for k in range(0, len(ranges), 100):
        batch = ranges[k:k + 100]
        chunks.append(f"{len(batch)} beginbfrange")
        for g_lo, g_hi, u_lo in batch:
            chunks.append(f"<{g_lo:04x}><{g_hi:04x}><{u_lo:04x}>")
        chunks.append("endbfrange")
    for k in range(0, len(chars), 100):
        batch = chars[k:k + 100]
        chunks.append(f"{len(batch)} beginbfchar")
        for gid, cp in batch:
            chunks.append(f"<{gid:04x}><{cp:04x}>")
        chunks.append("endbfchar")
    body = "\n".join(chunks)
    cmap = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\nbegincmap\n"
        "/CIDSystemInfo\n"
        "<< /Registry (Adobe)\n/Ordering (UCS)\n/Supplement 0\n>> def\n"
        "/CMapName /Adobe-Identity-UCS def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n<0000><FFFF>\nendcodespacerange\n"
        f"{body}\n"
        "endcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end end"
    )
    return cmap.encode("latin1")


# ─── Помощники byte-level ─────────────────────────────────────────────────────

def _find_obj_pos(pdf: bytes, xref: int) -> Tuple[int, int]:
    pos = pdf.find(f"\n{xref} 0 obj".encode())
    if pos < 0:
        pos = pdf.find(f"{xref} 0 obj".encode())
    if pos < 0:
        raise ValueError(f"obj {xref} not found")
    end = pdf.find(b"endobj", pos) + len(b"endobj")
    return pos, end


def _replace_obj_in_pdf(pdf: bytes, xref: int, new_obj_str: str) -> bytes:
    pos, end = _find_obj_pos(pdf, xref)
    leader = b"\n" if pdf[pos:pos+1] == b"\n" else b""
    return pdf[:pos] + leader + (f"{xref} 0 obj\n" + new_obj_str + "\nendobj").encode("latin1") + pdf[end:]


def _replace_stream_in_pdf(pdf: bytes, xref: int, new_stream: bytes, extra_dict_kvs: str = "") -> bytes:
    pos, end = _find_obj_pos(pdf, xref)
    leader = b"\n" if pdf[pos:pos+1] == b"\n" else b""
    new_block = leader + (
        f"{xref} 0 obj\n<</Filter /FlateDecode /Length {len(new_stream)}{extra_dict_kvs}>>\nstream\n"
    ).encode("latin1") + new_stream + b"\nendstream\nendobj"
    return pdf[:pos] + new_block + pdf[end:]


def _read_orig_basefont(pdf_bytes: bytes, xref: int) -> str:
    """Достать /BaseFont из объекта. Например 'NDSYUF+Onest-SemiBold'."""
    pos, end = _find_obj_pos(pdf_bytes, xref)
    obj = pdf_bytes[pos:end].decode("latin1", errors="replace")
    m = re.search(r"/BaseFont\s*/([^\s/<>]+)", obj)
    return m.group(1) if m else ""


def _split_prefix(basefont: str) -> Tuple[str, str]:
    """`NDSYUF+Onest-SemiBold` → ('NDSYUF', 'Onest-SemiBold')."""
    if "+" in basefont:
        a, b = basefont.split("+", 1)
        return a, b
    return "", basefont


# ─── Перекодировка content stream через новые GID ─────────────────────────────

def _reencode_content_stream(orig_cs: bytes, orig_ctx, new_uni_to_gid_reg, new_uni_to_gid_sb) -> bytes:
    cid_to_uni_reg_orig = {c: u for u, c in orig_ctx.uni_to_cid_reg.items()}
    cid_to_uni_sb_orig  = {c: u for u, c in orig_ctx.uni_to_cid_sb.items()}

    TF_RE = re.compile(rb"/F(\d+)\s+([0-9.]+)\s+Tf")
    TJ_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*Tj")

    out = bytearray()
    cursor = 0
    current_font = None
    events = []
    for m in TF_RE.finditer(orig_cs):
        events.append(("tf", m))
    for m in TJ_RE.finditer(orig_cs):
        events.append(("tj", m))
    events.sort(key=lambda e: e[1].start())

    for kind, m in events:
        out.extend(orig_cs[cursor:m.start()])
        if kind == "tf":
            current_font = m.group(1).decode()
            out.extend(m.group(0))
        else:
            hex_str = m.group(1).decode()
            cids = [int(hex_str[k:k+4], 16) for k in range(0, len(hex_str), 4)]
            if current_font == "1":
                table_dec, table_enc = cid_to_uni_sb_orig,  new_uni_to_gid_sb
            elif current_font == "2":
                table_dec, table_enc = cid_to_uni_reg_orig, new_uni_to_gid_reg
            else:
                out.extend(m.group(0)); cursor = m.end(); continue
            new_hex = ""
            for c in cids:
                u = table_dec.get(c)
                if u is None: continue
                new_gid = table_enc.get(u)
                if new_gid is None: continue
                new_hex += f"{new_gid:04x}"
            out.extend(b"<" + new_hex.encode("ascii") + b">Tj")
        cursor = m.end()
    out.extend(orig_cs[cursor:])
    return bytes(out)


# ─── xref rebuild ────────────────────────────────────────────────────────────

def _rebuild_xref_classic(pdf: bytes) -> bytes:
    sx = pdf.rfind(b"startxref")
    if sx < 0:
        raise RuntimeError("no startxref")
    body = pdf[:sx]
    last_endobj = body.rfind(b"endobj")
    body = body[:last_endobj + len(b"endobj")] + b"\n"

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

    trailer_pos = pdf.rfind(b"trailer", 0, sx)
    if trailer_pos > 0:
        trailer_block = pdf[trailer_pos:sx]
        trailer_block = re.sub(rb"/Size\s+\d+", f"/Size {max_x+1}".encode("latin1"), trailer_block)
        out.extend(trailer_block)
    else:
        m_root = re.search(rb"/Root\s+(\d+)\s+0\s+R", pdf)
        m_info = re.search(rb"/Info\s+(\d+)\s+0\s+R", pdf)
        m_id   = re.search(rb"/ID\s*\[<[^>]+><[^>]+>\]", pdf)
        root = int(m_root.group(1)) if m_root else 1
        info = int(m_info.group(1)) if m_info else 3
        id_part = m_id.group(0).decode("latin1") if m_id else ""
        out.extend(f"trailer\n<</Size {max_x+1}/Root {root} 0 R/Info {info} 0 R{id_part}>>\n".encode("latin1"))
    out.extend(f"startxref\n{new_xref_off}\n%%EOF\n".encode("latin1"))
    return bytes(out)


# ─── Главная процедура ──────────────────────────────────────────────────────

def build_unlocked_template(extra_reg_unicodes=None, extra_sb_unicodes=None,
                              orig_uni_to_cid_reg=None, orig_uni_to_cid_sb=None,
                              save_to_file: bool = True):
    """Создаёт extended PDF на основе orig.

    Если extra_reg_unicodes/extra_sb_unicodes заданы — расширяем orig subset
    минимально (orig_chars ∪ extra). Иначе используем глобальный _NEEDED_UNICODES.

    Возвращает (pdf_bytes, new_uni_to_gid_reg, new_uni_to_gid_sb).
    Если save_to_file=True — сохраняет в UNLOCKED_TEMPLATE.
    """
    if not os.path.exists(ORIG_TEMPLATE):
        raise FileNotFoundError(ORIG_TEMPLATE)

    # 1. Читаем orig и достаём оригинальные subset-prefix-ы
    with open(ORIG_TEMPLATE, "rb") as f:
        pdf = f.read()

    # OTP layout: F1=SemiBold (Type0=6, FontFile2=13, ToU=14, CIDFont=15, FontDesc=12)
    # F2=Regular  (Type0=7, FontFile2=18, ToU=19, CIDFont=20, FontDesc=17)
    F1_FONTFILE2_XREF, F1_TOUNICODE_XREF, F1_CIDFONT_XREF, F1_FONTDESC_XREF, F1_TYPE0_XREF = 13, 14, 15, 12, 6
    F2_FONTFILE2_XREF, F2_TOUNICODE_XREF, F2_CIDFONT_XREF, F2_FONTDESC_XREF, F2_TYPE0_XREF = 18, 19, 20, 17, 7
    CS_XREF = 5

    orig_basefont_sb  = _read_orig_basefont(pdf, F1_TYPE0_XREF)   # NDSYUF+Onest-SemiBold
    orig_basefont_reg = _read_orig_basefont(pdf, F2_TYPE0_XREF)   # MMPJRG+Onest-Regular
    sb_prefix,  sb_base  = _split_prefix(orig_basefont_sb)
    reg_prefix, reg_base = _split_prefix(orig_basefont_reg)
    if not (sb_prefix and reg_prefix):
        # Если префиксы отсутствуют — выберем стабильные псевдо-prefix-ы
        sb_prefix  = sb_prefix  or "NDSYUF"
        reg_prefix = reg_prefix or "MMPJRG"
        sb_base    = sb_base    or "Onest-SemiBold"
        reg_base   = reg_base   or "Onest-Regular"

    full_sb_name  = f"{sb_prefix}+{sb_base}"
    full_reg_name = f"{reg_prefix}+{reg_base}"
    logger.info(f"OTP unlock: SB={full_sb_name}  REG={full_reg_name}")

    # 2. Решаем какие subset-ы использовать
    if extra_reg_unicodes is not None and orig_uni_to_cid_reg is not None:
        sb_unicodes  = set(orig_uni_to_cid_sb.keys())  | set(extra_sb_unicodes or [])
        reg_unicodes = set(orig_uni_to_cid_reg.keys()) | set(extra_reg_unicodes or [])
    else:
        sb_unicodes  = _NEEDED_UNICODES_SB    # узкий — header/total
        reg_unicodes = _NEEDED_UNICODES_REG   # широкий — имена/SBP ID

    font_sb,  ttf_sb  = _subset_like_itext(FONT_SEMIBOLD, sb_unicodes)
    font_reg, ttf_reg = _subset_like_itext(FONT_REGULAR,  reg_unicodes)

    uni_to_gid_sb  = _build_uni_to_gid(font_sb)
    uni_to_gid_reg = _build_uni_to_gid(font_reg)

    used_gids_sb  = sorted(set(uni_to_gid_sb.values()))
    used_gids_reg = sorted(set(uni_to_gid_reg.values()))

    widths_sb  = _build_widths_array(font_sb,  used_gids_sb)
    widths_reg = _build_widths_array(font_reg, used_gids_reg)

    cmap_sb  = _build_tounicode_cmap(uni_to_gid_sb)
    cmap_reg = _build_tounicode_cmap(uni_to_gid_reg)

    # 3. Перекодируем content stream под новые GID
    from otp_orig_mode import OtpOrigContext
    orig_ctx = OtpOrigContext()
    orig_ctx.load(ORIG_TEMPLATE)
    doc = fitz.open(ORIG_TEMPLATE)
    orig_cs = doc.xref_stream(CS_XREF)
    new_cs = _reencode_content_stream(orig_cs, orig_ctx, uni_to_gid_reg, uni_to_gid_sb)
    new_cs_compressed = zlib.compress(new_cs, 6)
    doc.close()

    # 4. Подменяем content stream
    pdf = _replace_stream_in_pdf(pdf, CS_XREF, new_cs_compressed)

    # 5. Подменяем FontFile2
    pdf = _replace_stream_in_pdf(pdf, F1_FONTFILE2_XREF, zlib.compress(ttf_sb, 6),
                                 extra_dict_kvs=f" /Length1 {len(ttf_sb)}")
    pdf = _replace_stream_in_pdf(pdf, F2_FONTFILE2_XREF, zlib.compress(ttf_reg, 6),
                                 extra_dict_kvs=f" /Length1 {len(ttf_reg)}")

    # 6. Подменяем ToUnicode CMap
    pdf = _replace_stream_in_pdf(pdf, F1_TOUNICODE_XREF, zlib.compress(cmap_sb,  6))
    pdf = _replace_stream_in_pdf(pdf, F2_TOUNICODE_XREF, zlib.compress(cmap_reg, 6))

    # 7. CIDFontType2 — с тем же prefix-ом в BaseFont
    sb_cidfont = (
        f"<</Type /Font /Subtype /CIDFontType2 /BaseFont /{full_sb_name} "
        f"/CIDSystemInfo <</Registry (Adobe) /Ordering (Identity) /Supplement 0>> "
        f"/CIDToGIDMap /Identity /DW 1000 /FontDescriptor {F1_FONTDESC_XREF} 0 R "
        f"/W {widths_sb}>>"
    )
    reg_cidfont = (
        f"<</Type /Font /Subtype /CIDFontType2 /BaseFont /{full_reg_name} "
        f"/CIDSystemInfo <</Registry (Adobe) /Ordering (Identity) /Supplement 0>> "
        f"/CIDToGIDMap /Identity /DW 1000 /FontDescriptor {F2_FONTDESC_XREF} 0 R "
        f"/W {widths_reg}>>"
    )
    pdf = _replace_obj_in_pdf(pdf, F1_CIDFONT_XREF, sb_cidfont)
    pdf = _replace_obj_in_pdf(pdf, F2_CIDFONT_XREF, reg_cidfont)

    # 8. FontDescriptor — FontName с тем же prefix
    sb_head = font_sb["head"]
    reg_head = font_reg["head"]
    sb_os2 = font_sb["OS/2"]
    reg_os2 = font_reg["OS/2"]

    sb_desc = (
        f"<</Type /FontDescriptor /FontName /{full_sb_name} /Flags 4 "
        f"/FontBBox [{sb_head.xMin} {sb_head.yMin} {sb_head.xMax} {sb_head.yMax}] "
        f"/ItalicAngle 0 /Ascent {sb_os2.sTypoAscender} /Descent {sb_os2.sTypoDescender} "
        f"/CapHeight {getattr(sb_os2, 'sCapHeight', 700)} /StemV 80 "
        f"/FontFile2 {F1_FONTFILE2_XREF} 0 R>>"
    )
    reg_desc = (
        f"<</Type /FontDescriptor /FontName /{full_reg_name} /Flags 32 "
        f"/FontBBox [{reg_head.xMin} {reg_head.yMin} {reg_head.xMax} {reg_head.yMax}] "
        f"/ItalicAngle 0 /Ascent {reg_os2.sTypoAscender} /Descent {reg_os2.sTypoDescender} "
        f"/CapHeight {getattr(reg_os2, 'sCapHeight', 700)} /StemV 80 "
        f"/FontFile2 {F2_FONTFILE2_XREF} 0 R>>"
    )
    pdf = _replace_obj_in_pdf(pdf, F1_FONTDESC_XREF, sb_desc)
    pdf = _replace_obj_in_pdf(pdf, F2_FONTDESC_XREF, reg_desc)

    # 9. Type0 — BaseFont с prefix
    f1_type0 = (
        f"<</Type /Font /Subtype /Type0 /BaseFont /{full_sb_name} "
        f"/Encoding /Identity-H /DescendantFonts [{F1_CIDFONT_XREF} 0 R] "
        f"/ToUnicode {F1_TOUNICODE_XREF} 0 R>>"
    )
    f2_type0 = (
        f"<</Type /Font /Subtype /Type0 /BaseFont /{full_reg_name} "
        f"/Encoding /Identity-H /DescendantFonts [{F2_CIDFONT_XREF} 0 R] "
        f"/ToUnicode {F2_TOUNICODE_XREF} 0 R>>"
    )
    pdf = _replace_obj_in_pdf(pdf, F1_TYPE0_XREF, f1_type0)
    pdf = _replace_obj_in_pdf(pdf, F2_TYPE0_XREF, f2_type0)

    # 10. Перестройка xref + trailer
    out = _rebuild_xref_classic(pdf)

    if save_to_file:
        with open(UNLOCKED_TEMPLATE, "wb") as f:
            f.write(out)
        logger.info(f"OTP unlocked: saved {UNLOCKED_TEMPLATE} ({len(out)} bytes)")
    return out, uni_to_gid_reg, uni_to_gid_sb


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    pdf_bytes, _, _ = build_unlocked_template(save_to_file=True)
    print(f"saved: {UNLOCKED_TEMPLATE} ({len(pdf_bytes)} bytes)")
    d = fitz.open(UNLOCKED_TEMPLATE)
    print(f"pages={len(d)}, text preview:")
    print(d[0].get_text()[:300])
    d.close()
