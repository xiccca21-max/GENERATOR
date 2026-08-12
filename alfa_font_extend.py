"""Расширение subset Tahoma в Alfa PDF — swap/inject как у T-Bank SBP."""
from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import zlib
from copy import deepcopy
from io import BytesIO
from typing import Dict, List, Optional, Set, Tuple

import fitz
from fontTools.ttLib import TTFont

from alfa_orig_mode import (
    AlfaOrigContext,
    _find_stream_pos_for_xref,
    _parse_widths,
    _pad_to_compressed_size,
    _zlib_level_from_header,
)

logger = logging.getLogger(__name__)


def _collect_alfa_used_cids(stream: bytes) -> Set[int]:
    """CID из hex-строк `<....>Tj` в Alfa content stream."""
    used: Set[int] = set()
    for m in re.finditer(rb"<([0-9A-Fa-f]+)>", stream):
        hx = m.group(1)
        for i in range(0, len(hx), 4):
            used.add(int(hx[i : i + 4], 16))
    return used


def _save_oracle_ttf(font: TTFont, template: Optional[TTFont] = None) -> bytes:
    """fontTools.save; preserve Oracle head flags + Alfa created/modified epochs.

    fontTools defaults break Proton HARD checks:
      - recalcTimestamp=True → head.modified becomes \"now\"
      - loca.compile → short format (indexToLocFormat=0) when glyf is small
      - recalcBBoxes=True → head xMin.. drift vs frozen /FontBBox
    Oracle Alfa F1 requires flags=27, indexToLocFormat=1, and
    created=2931866140 ⇒ modified=3170332026.
    """
    import array

    _ALFA_CREATED = 2931866140
    _ALFA_MODIFIED = 3170332026
    font.recalcTimestamp = False
    font.recalcBBoxes = False
    font["head"].flags = 27
    if template is not None:
        th = template["head"]
        font["head"].macStyle = int(getattr(th, "macStyle", font["head"].macStyle))
        font["head"].created = int(getattr(th, "created", font["head"].created))
        font["head"].modified = int(getattr(th, "modified", font["head"].modified))
        # Keep donor head bounds so /FontBBox stays in sync without Length rebuild.
        for attr in ("xMin", "yMin", "xMax", "yMax"):
            if hasattr(th, attr):
                setattr(font["head"], attr, int(getattr(th, attr)))
    created = int(getattr(font["head"], "created", 0) or 0)
    if created == _ALFA_CREATED:
        font["head"].created = _ALFA_CREATED
        font["head"].modified = _ALFA_MODIFIED
    font["head"].indexToLocFormat = 1

    # Force long loca (Oracle indexToLocFormat=1) — default compile picks short.
    loca = font["loca"]
    _orig_compile = loca.compile

    def _compile_long(ttFont):
        try:
            locations = array.array("I", loca.locations)
        except AttributeError:
            loca.set([])
            locations = array.array("I", loca.locations)
        ttFont["head"].indexToLocFormat = 1
        if sys.byteorder != "big":
            locations.byteswap()
        return locations.tobytes()

    loca.compile = _compile_long  # type: ignore[method-assign]
    try:
        out = BytesIO()
        font.save(out)
        return out.getvalue()
    finally:
        loca.compile = _orig_compile  # type: ignore[method-assign]


def _head_pdf_bbox(tt: TTFont) -> Tuple[int, int, int, int]:
    head = tt["head"]
    upem = float(head.unitsPerEm or 2048)
    return (
        int(round(head.xMin * 1000.0 / upem)),
        int(round(head.yMin * 1000.0 / upem)),
        int(round(head.xMax * 1000.0 / upem)),
        int(round(head.yMax * 1000.0 / upem)),
    )


def _patch_font_descriptor_bbox(pdf: bytearray, ff2_xref: int, ttf: bytes) -> bool:
    """Синхронизировать /FontBBox с head после правки FontFile2."""
    from tbank_orig_mode import find_object_range
    from tbank_sbp_stealth import _replace_byte_range_and_rebuild

    try:
        tt = TTFont(BytesIO(ttf))
        x0, y0, x1, y1 = _head_pdf_bbox(tt)
    except Exception:
        return False
    bbox_s = f"/FontBBox [{x0} {y0} {x1} {y1}]"
    ok_any = False
    for xref in range(1, 40):
        rng = find_object_range(bytes(pdf), xref)
        if not rng:
            continue
        obj = bytes(pdf[rng[0] : rng[1]]).decode("latin1", "replace")
        if f"/FontFile2 {ff2_xref} 0 R" not in obj or "/FontBBox" not in obj:
            continue
        new_obj = re.sub(
            r"/FontBBox\s*\[\s*-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s*\]",
            bbox_s,
            obj,
            count=1,
        )
        if new_obj == obj:
            continue
        nb = new_obj.encode("latin1")
        if len(nb) == rng[1] - rng[0]:
            pdf[rng[0] : rng[1]] = nb
        else:
            patched = _replace_byte_range_and_rebuild(bytes(pdf), rng[0], rng[1], nb)
            if patched is None:
                return False
            pdf[:] = patched
        ok_any = True
    return ok_any


def _prune_oracle_tounicode(tu_bytes: bytes, keep: Set[int]) -> bytes:
    text = tu_bytes.decode("latin1", "replace")
    nl = "\r\n" if "\r\n" in text else "\n"
    m = re.search(r"(\d+)\s+beginbfchar", text)
    if not m:
        return tu_bytes
    pairs = re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", text)
    kept = [(a, b) for a, b in pairs if int(a, 16) in keep]
    if len(kept) == len(pairs):
        return tu_bytes
    body = "".join(f"<{a}> <{b}>{nl}" for a, b in kept)
    new_text = re.sub(
        r"\d+\s+beginbfchar.*?endbfchar",
        f"{len(kept)} beginbfchar{nl}{body}endbfchar",
        text,
        count=1,
        flags=re.S,
    )
    return new_text.encode("latin1")


def _prune_oracle_w_array(obj: str, keep: Set[int]) -> Optional[str]:
    br = _oracle_w_bracket(obj)
    if not br:
        return None
    widths = _parse_widths(obj)
    if not widths:
        return None
    filtered = {cid: w for cid, w in widths.items() if cid in keep}
    if len(filtered) == len(widths):
        return obj
    parts = [obj[: br[0] + 1]]
    for cid in sorted(filtered):
        parts.append(f" {cid} [ {filtered[cid]} ]")
    parts.append("]")
    parts.append(obj[br[1] :])
    return "".join(parts)


def _neutralize_orphan_tounicode(tu_bytes: bytes, keep: Set[int]) -> bytes:
    """Unused printable CIDs → U+0000 in ToUnicode (same bfchar count/size).

    Full prune of ToUnicode/W → OnlyPDF «чек не распознан».
    Proton ALFA_ORACLE_FONT_SUBSET_CLOSURE only flags *printable* orphans.
    """
    text = tu_bytes.decode("latin1", "replace")
    pairs = list(re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", text))
    if not pairs:
        return tu_bytes
    out = text
    # Replace from the end so offsets stay valid.
    for m in reversed(pairs):
        cid = int(m.group(1), 16)
        if cid in keep or cid == 0:
            continue
        uni_hex = m.group(2)
        try:
            cp = int(uni_hex, 16)
        except ValueError:
            continue
        if cp <= 0x20 or cp == 0xA0:
            continue
        # Keep hex width identical (usually 4).
        zero = "0" * len(uni_hex)
        start, end = m.start(2), m.end(2)
        out = out[:start] + zero + out[end:]
    return out.encode("latin1") if out != text else tu_bytes


def _closure_fix_alfa_font(pdf: bytearray, stream: bytes) -> bytes:
    """Закрыть Oracle subset closure без удаления CID (OnlyPDF-safe)."""
    active = _collect_alfa_used_cids(stream)
    if not active:
        return bytes(pdf)
    keep = set(active)
    keep.add(0)
    refs = _load_font_xrefs_from_bytes(bytes(pdf))
    if not refs:
        return bytes(pdf)
    tu_dec = _tu_read_decompressed(bytes(pdf), refs["tu"])
    tu_new = _neutralize_orphan_tounicode(tu_dec, keep)
    if tu_new != tu_dec:
        if not _patch_tu_decompressed(pdf, refs["tu"], tu_new):
            logger.warning("Alfa closure-fix ToUnicode patch failed")
            return bytes(pdf)
        logger.info(
            "Alfa closure-fix: neutralized orphan printable CIDs (keep %d used)",
            len(active),
        )
    return bytes(pdf)


def _prune_alfa_font_subset(pdf: bytearray, stream: bytes) -> bytes:
    """DEPRECATED for emit: OnlyPDF «не распознан». Use _closure_fix_alfa_font."""
    return _closure_fix_alfa_font(pdf, stream)


def _load_font_xrefs_from_bytes(pdf_bytes: bytes) -> Optional[Dict[str, int]]:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    ff2 = tu = cid = 0
    try:
        for xref in range(1, doc.xref_length()):
            obj = doc.xref_object(xref)
            if "/Subtype /Type0" in obj:
                m_tu = re.search(r"/ToUnicode (\d+)", obj)
                m_cid = re.search(r"/DescendantFonts\s*\[\s*(\d+)", obj)
                if m_tu:
                    tu = int(m_tu.group(1))
                if m_cid:
                    cid = int(m_cid.group(1))
            if "/FontFile2" in obj:
                m_ff = re.search(r"/FontFile2 (\d+)", obj)
                if m_ff:
                    ff2 = int(m_ff.group(1))
    finally:
        doc.close()
    if not (ff2 and tu and cid):
        return None
    return {"ff2": ff2, "tu": tu, "cid": cid}


def _patch_length_and_rebuild(pdf: bytearray, cs: int, ce: int, new_stream: bytes) -> Optional[bytes]:
    from tbank_sbp_stealth import _patch_length_and_rebuild as _tb_patch

    out = _tb_patch(pdf, cs, ce, new_stream)
    return bytes(out) if out is not None else None


def _patch_cidfont_w(pdf: bytearray, cid_xref: int, new_w: str) -> bool:
    from tbank_sbp_stealth import _patch_cidfont_w as _tb_w

    return _tb_w(pdf, cid_xref, new_w)


def _load_font_xrefs(path: str) -> Optional[Dict[str, int]]:
    try:
        doc = fitz.open(path)
    except Exception:
        return None
    ff2 = tu = cid = 0
    for xref in range(1, doc.xref_length()):
        obj = doc.xref_object(xref)
        if "/Subtype /Type0" in obj:
            m_tu = re.search(r"/ToUnicode (\d+)", obj)
            m_cid = re.search(r"/DescendantFonts\s*\[\s*(\d+)", obj)
            if m_tu:
                tu = int(m_tu.group(1))
            if m_cid:
                cid = int(m_cid.group(1))
        if "/FontFile2" in obj:
            m_ff = re.search(r"/FontFile2 (\d+)", obj)
            if m_ff:
                ff2 = int(m_ff.group(1))
    doc.close()
    if not (ff2 and tu and cid):
        return None
    return {"ff2": ff2, "tu": tu, "cid": cid}


def _needed_codepoints(text: str) -> Set[int]:
    out: Set[int] = set()
    for ch in text or "":
        if ch in ("\n", "\r", "\t"):
            continue
        out.add(0x00A0 if ch == " " else ord(ch))
    return out


def _ff2_read_decompressed(pdf_bytes: bytes, ff2_xref: int) -> bytes:
    """FontFile2 в Alfa PDF — FlateDecode; fontTools/fitz нужен распакованный TTF."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc.xref_stream(ff2_xref)
    finally:
        doc.close()


def _ff2_contours(ff2_ttf: bytes, cid: int) -> int:
    try:
        ft = TTFont(BytesIO(ff2_ttf))
        go = ft.getGlyphOrder()
        if cid < 0 or cid >= len(go):
            return 0
        return int(getattr(ft["glyf"][go[cid]], "numberOfContours", 0) or 0)
    except Exception:
        return 0


def _glyph_slot_has_ink(ff2_ttf: bytes, cid: int) -> bool:
    """Пустой слот nc=0; simple nc>0; composite nc=-1 — все кроме 0 валидны."""
    return _ff2_contours(ff2_ttf, cid) != 0


def _char_codepoint(ch: str) -> int:
    return 0x00A0 if ch == " " else ord(ch)


def _tu_read_decompressed(pdf_bytes: bytes, tu_xref: int) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc.xref_stream(tu_xref)
    finally:
        doc.close()


def _patch_tu_decompressed(pdf: bytearray, tu_xref: int, new_tu: bytes) -> bool:
    from alfa_orig_mode import _unreproducible_flate

    pos = _find_stream_pos_for_xref(bytes(pdf), tu_xref)
    if not pos:
        return False
    cs, ce = pos
    level = _zlib_level_from_header(bytes(pdf)[cs : cs + 2])
    # Как у Oracle: не zlib.compress (иначе MIXED_ZLIB с image-потоками).
    compressed = _unreproducible_flate(new_tu, level)
    patched = _patch_length_and_rebuild(pdf, cs, ce, compressed)
    if patched is None:
        return False
    pdf[:] = patched
    return True


def _char_glyph_ok(ctx: AlfaOrigContext, ff2_ttf: bytes, ch: str) -> bool:
    """ToUnicode + /W + непустой glyf (пробелы — только CMap+/W)."""
    cp = _char_codepoint(ch)
    cid = ctx.uni_to_cid.get(cp)
    if cid is None:
        return False
    if ctx.cid_to_uni.get(cid) != cp:
        return False
    if cid not in ctx.widths:
        return False
    if ch in (" ", "\t", "\n", "\r", "\u00a0"):
        return True
    return _glyph_slot_has_ink(ff2_ttf, cid)


def _glyphs_ok_for_text(
    ctx: AlfaOrigContext, pdf_bytes: bytes, text: str,
) -> Tuple[bool, List[str]]:
    if not ctx.ff2_xref:
        return False, list(text)
    try:
        ff2 = _ff2_read_decompressed(pdf_bytes, ctx.ff2_xref)
    except Exception:
        return False, list(text)
    bad: List[str] = []
    for ch in text or "":
        if ch in ("\n", "\r", "\t"):
            continue
        if not _char_glyph_ok(ctx, ff2, ch):
            bad.append(ch)
    return (len(bad) == 0, bad)


def _patch_ff2_decompressed(pdf: bytearray, ff2_xref: int, new_ttf: bytes) -> bool:
    from alfa_orig_mode import _unreproducible_flate

    pos = _find_stream_pos_for_xref(bytes(pdf), ff2_xref)
    if not pos:
        return False
    cs, ce = pos
    level = _zlib_level_from_header(bytes(pdf)[cs : cs + 2])
    # Не zlib.compress: иначе content/font «canonical», image — нет → MIXED_ZLIB.
    compressed = _unreproducible_flate(new_ttf, level)
    patched = _patch_length_and_rebuild(pdf, cs, ce, compressed)
    if patched is None:
        return False
    pdf[:] = patched
    # /Length1 должен = len(decoded TTF)
    ok_len = _patch_fontfile2_length1(pdf, ff2_xref, len(new_ttf))
    return ok_len


def _patch_fontfile2_length1(pdf: bytearray, ff2_xref: int, length1: int) -> bool:
    """Обновить /Length1 в FontDescriptor (и в dict FontFile2, если есть)."""
    from tbank_orig_mode import find_object_range
    from tbank_sbp_stealth import _replace_byte_range_and_rebuild

    raw = bytes(pdf)
    # FontFile2 object may itself contain /Length1
    targets = [ff2_xref]
    # find FontDescriptor that points to this FontFile2
    for xref in range(1, 40):
        rng = find_object_range(raw, xref)
        if not rng:
            continue
        obj = raw[rng[0] : rng[1]]
        if f"/FontFile2 {ff2_xref} 0 R".encode("ascii") in obj or f"/FontFile2 {ff2_xref} 0 R".encode("latin1") in obj:
            targets.append(xref)
    ok_any = False
    for xref in targets:
        rng = find_object_range(bytes(pdf), xref)
        if not rng:
            continue
        o0, o1 = rng
        obj = bytes(pdf[o0:o1]).decode("latin1", "replace")
        if "/Length1" not in obj:
            continue
        new_obj = re.sub(r"/Length1\s+\d+", f"/Length1 {int(length1)}", obj, count=1)
        if new_obj == obj:
            continue
        nb = new_obj.encode("latin1")
        if len(nb) == o1 - o0:
            pdf[o0:o1] = nb
            ok_any = True
            continue
        patched = _replace_byte_range_and_rebuild(bytes(pdf), o0, o1, nb)
        if patched is None:
            return False
        pdf[:] = patched
        ok_any = True
    return ok_any or True  # ok if no Length1 existed


def _find_font_donor(paths: List[str], text: str) -> Optional[str]:
    """PDF из корпуса, чей Tahoma subset покрывает весь text с валидными glyf."""
    best: Optional[str] = None
    best_n = 10**9
    for path in paths:
        ctx = AlfaOrigContext()
        if not ctx.load(path):
            continue
        ok, _ = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, text)
        if not ok:
            continue
        n = len(ctx.uni_to_cid)
        if n < best_n:
            best_n = n
            best = path
    return best


def _find_donor_for_char(paths: List[str], ch: str) -> Optional[str]:
    for path in paths:
        ctx = AlfaOrigContext()
        if not ctx.load(path):
            continue
        ok, _ = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, ch)
        if ok:
            return path
    return None


def _stream_labels_unchanged(before: AlfaOrigContext, after: AlfaOrigContext) -> bool:
    """Статичный content stream не трогаем — ToUnicode/glyf не должны ломать decode."""
    stream = bytes(before.stream)
    for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*Tj", stream):
        hx = m.group(1)
        if before.dec(hx) != after.dec(hx):
            return False
    return True


def swap_alfa_font(base_path: str, font_donor_path: str) -> Optional[str]:
    """DEPRECATED: подмена ToUnicode ломает статичные подписи shell (разные CID-карты).

    Оставлено для совместимости; не копирует ToUnicode//W — только FontFile2 при
    идентичной CID-карте.
    """
    if base_path == font_donor_path:
        return base_path
    ctx_b = AlfaOrigContext()
    ctx_d = AlfaOrigContext()
    if not ctx_b.load(base_path) or not ctx_d.load(font_donor_path):
        return None
    if ctx_b.uni_to_cid != ctx_d.uni_to_cid:
        logger.warning(
            "Alfa font swap skipped: CID map mismatch %s vs %s",
            os.path.basename(base_path),
            os.path.basename(font_donor_path),
        )
        return None
    refs_b = _load_font_xrefs(base_path)
    refs_d = _load_font_xrefs(font_donor_path)
    if not refs_b or not refs_d:
        return None

    with open(font_donor_path, "rb") as f:
        donor_raw = f.read()
    with open(base_path, "rb") as f:
        pdf = bytearray(f.read())

    def _copy_stream(bx: int, dx: int) -> bool:
        src = _find_stream_pos_for_xref(donor_raw, dx)
        dst = _find_stream_pos_for_xref(bytes(pdf), bx)
        if src is None or dst is None:
            return False
        patched = _patch_length_and_rebuild(pdf, dst[0], dst[1], donor_raw[src[0] : src[1]])
        if patched is None:
            return False
        pdf[:] = patched
        return True

    if not _copy_stream(refs_b["ff2"], refs_d["ff2"]):
        return None

    fd, out = tempfile.mkstemp(suffix=".pdf", prefix="alfa_font_swap_")
    os.close(fd)
    with open(out, "wb") as f:
        f.write(bytes(pdf))
    logger.info(
        "Alfa font swap (ff2 only): %s -> %s",
        os.path.basename(base_path),
        os.path.basename(font_donor_path),
    )
    return out


def _append_bfchar_to_tounicode(tu_bytes: bytes, cid: int, cp: int) -> Optional[bytes]:
    text = tu_bytes.decode("latin1", "replace")
    if re.search(rf"<{cid:04X}>\s*<{cp:04X}>", text):
        return tu_bytes
    m = re.search(r"(\d+)\s+beginbfchar", text)
    if not m:
        return None
    old_n = int(m.group(1))
    nl = "\r\n" if "\r\n" in text else "\n"
    new_line = f"<{cid:04X}> <{cp:04X}>{nl}"
    insert_at = text.find("endbfchar")
    if insert_at < 0:
        return None
    text = text[:insert_at] + new_line + text[insert_at:]
    text = text.replace(f"{old_n} beginbfchar", f"{old_n + 1} beginbfchar", 1)
    return text.encode("latin1")


def _component_cids(ff2: bytes) -> Set[int]:
    """CID-ы, на которые ссылаются composite — их нельзя перезаписывать."""
    ft = TTFont(BytesIO(ff2))
    go = ft.getGlyphOrder()
    name_to_cid = {n: i for i, n in enumerate(go)}
    used: Set[int] = set()
    glyf = ft["glyf"]
    for gname in go:
        g = glyf[gname]
        if getattr(g, "numberOfContours", 0) != -1:
            continue
        for comp in getattr(g, "components", []) or []:
            cid = name_to_cid.get(comp.glyphName)
            if cid is not None:
                used.add(cid)
    return used


def _as_simple_glyph(src_font: TTFont, gname: str):
    """Composite → simple outline (без чужих component-имён в dest font)."""
    glyf = src_font["glyf"]
    g = deepcopy(glyf[gname])
    nc = getattr(g, "numberOfContours", 0)
    if nc > 0:
        return g
    if nc == 0:
        return None
    # Fully decompose nested composites (notably й/Й in some Tahoma subsets).
    # The older one-level path below cannot flatten a component which is
    # itself composite and caused exact charset extension to fail.
    try:
        from fontTools.pens.recordingPen import DecomposingRecordingPen
        from fontTools.pens.ttGlyphPen import TTGlyphPen

        glyph_set = src_font.getGlyphSet()
        recording = DecomposingRecordingPen(glyph_set)
        glyph_set[gname].draw(recording)
        out_pen = TTGlyphPen(None)
        recording.replay(out_pen)
        flattened = out_pen.glyph()
        if getattr(flattened, "numberOfContours", 0) > 0:
            flattened.recalcBounds(src_font["glyf"])
            return flattened
    except Exception:
        pass
    # composite: flatten один уровень (типичный Alfa subset)
    comps = list(getattr(g, "components", []) or [])
    if not comps:
        return None
    if len(comps) == 1:
        info = comps[0].getComponentInfo()
        cname, transform = info[0], info[1]
        # transform: (xx, xy, yx, yy, dx, dy)
        xx, xy, yx, yy, dx, dy = transform
        leaf = deepcopy(glyf[cname])
        if getattr(leaf, "numberOfContours", 0) <= 0:
            return None
        identity = (xx, xy, yx, yy, dx, dy) == (1, 0, 0, 1, 0, 0)
        if identity:
            return leaf
        # масштабируем/сдвигаем координаты
        coords = []
        for x, y in leaf.coordinates:
            nx = xx * x + xy * y + dx
            ny = yx * x + yy * y + dy
            coords.append((int(round(nx)), int(round(ny))))
        leaf.coordinates = coords
        leaf.recalcBounds(glyf)
        return leaf
    # несколько компонент — склеиваем через pen
    try:
        from fontTools.pens.ttGlyphPen import TTGlyphPen
        from fontTools.pens.transformPen import TransformPen

        pen = TTGlyphPen(None)
        for comp in comps:
            cname, transform = comp.getComponentInfo()
            leaf = glyf[cname]
            if getattr(leaf, "numberOfContours", 0) <= 0:
                continue
            tpen = TransformPen(pen, transform)
            leaf.draw(tpen)
        out = pen.glyph()
        if getattr(out, "numberOfContours", 0) <= 0:
            return None
        return out
    except Exception:
        return None


def _copy_glyf_slot(dst_ff2: bytes, src_ff2: bytes, src_cid: int, dst_cid: int) -> Optional[bytes]:
    if src_cid == dst_cid and dst_ff2 == src_ff2:
        return dst_ff2
    dst = TTFont(BytesIO(dst_ff2))
    src = TTFont(BytesIO(src_ff2))
    template = TTFont(BytesIO(dst_ff2))
    # Прогреть таблицы до изменения numGlyphs.
    _ = dst["glyf"]
    _ = dst["hmtx"]
    _ = dst["maxp"]
    dst_go = list(dst.getGlyphOrder())
    src_go = src.getGlyphOrder()
    if src_cid >= len(src_go) or dst_cid < 0:
        return None
    simple = _as_simple_glyph(src, src_go[src_cid])
    if simple is None:
        return None
    src_aw_lsb = src["hmtx"].metrics[src_go[src_cid]]

    if dst_cid > len(dst_go):
        return None
    if dst_cid == len(dst_go):
        new_name = f"glyph{dst_cid:05d}"
        while new_name in dst_go:
            new_name += "x"
        dst_go.append(new_name)
        dst.setGlyphOrder(dst_go)
        dst["glyf"][new_name] = simple
        dst["hmtx"].metrics[new_name] = src_aw_lsb
        dst["maxp"].numGlyphs = len(dst_go)
    else:
        dst_name = dst_go[dst_cid]
        dst["glyf"][dst_name] = simple
        dst["hmtx"].metrics[dst_name] = src_aw_lsb

    return _save_oracle_ttf(dst, template)


def _alloc_cid(
    ctx: AlfaOrigContext,
    ff2: bytes,
    stream_used: Optional[Set[int]] = None,
) -> Optional[int]:
    used = set(ctx.uni_to_cid.values())
    reserved = set(stream_used or ())
    components = _component_cids(ff2)
    ft = TTFont(BytesIO(ff2))
    n = len(ft.getGlyphOrder())
    glyf = ft["glyf"]
    go = ft.getGlyphOrder()
    for cid in range(n):
        if cid in used or cid in components or cid == 0 or cid in reserved:
            continue
        g = glyf[go[cid]]
        if getattr(g, "numberOfContours", 0) == 0:
            return cid
    if n < 255:
        return n
    return None


def _oracle_w_bracket(obj: str) -> Optional[Tuple[int, int]]:
    """Диапазон `[...]` у /W с учётом вложенности."""
    idx = obj.find("/W")
    while idx >= 0:
        ahead = obj[idx + 2 : idx + 3]
        if ahead in (" ", "\t", "\n", "\r", "["):
            break
        idx = obj.find("/W", idx + 1)
    if idx < 0:
        return None
    j = idx + 2
    while j < len(obj) and obj[j] != "[":
        j += 1
    if j >= len(obj):
        return None
    depth = 0
    end = j
    while end < len(obj):
        c = obj[end]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return j, end + 1
        end += 1
    return None


def _append_oracle_cid_width(pdf: bytearray, cid_xref: int, cid: int, width: int) -> bool:
    """Дописать `cid [ width ]` в /W, не пересобирая массив в compact-формат."""
    from tbank_orig_mode import find_object_range

    obj_rng = find_object_range(bytes(pdf), cid_xref)
    if not obj_rng:
        return False
    o_start, o_end = obj_rng
    raw_obj = bytes(pdf[o_start:o_end]).decode("latin1", errors="replace")
    br = _oracle_w_bracket(raw_obj)
    if not br:
        return False
    b0, b1 = br
    body = raw_obj[b0:b1]  # [...]
    # уже есть запись для этого CID — не трогаем формат
    if re.search(rf"(?:^|[\s\[]){cid}\s*\[", body):
        return True
    # Oracle style: " 42 [ 600 ]" перед закрывающей ]
    entry = f" {cid} [ {int(width)} ]"
    # сохраняем перенос, если массив многострочный
    if "\n" in body:
        insert = entry + "\n"
    else:
        insert = entry
    new_body = body[:-1] + insert + "]"
    new_obj = raw_obj[:b0] + new_body + raw_obj[b1:]
    new_obj_b = new_obj.encode("latin1")
    if len(new_obj_b) == o_end - o_start:
        pdf[o_start:o_end] = new_obj_b
        return True
    from tbank_sbp_stealth import _replace_byte_range_and_rebuild

    patched = _replace_byte_range_and_rebuild(bytes(pdf), o_start, o_end, new_obj_b)
    if patched is None:
        return False
    pdf[:] = patched
    return True


def _copy_oracle_w_array(pdf: bytearray, dst_cid_xref: int, donor_obj: str) -> bool:
    """Скопировать /W [...] донора as-is (Oracle pretty-print)."""
    br = _oracle_w_bracket(donor_obj)
    if not br:
        return False
    w_bracket = donor_obj[br[0] : br[1]]
    return _patch_cidfont_w(pdf, dst_cid_xref, w_bracket)


def _find_best_partial_donor(paths: List[str], text: str) -> Optional[str]:
    """Донор с максимальным покрытием text (даже если не 100%)."""
    best: Optional[str] = None
    best_score = -1
    need = [ch for ch in text or "" if ch not in ("\n", "\r", "\t")]
    if not need:
        return None
    for path in paths:
        ctx = AlfaOrigContext()
        if not ctx.load(path):
            continue
        ok, miss = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, text)
        score = len(need) - len(miss)
        if score > best_score:
            best_score = score
            best = path
        if ok:
            return path
    return best


def _install_simple_glyph(
    dst_ff2: bytes, dst_cid: int, simple, aw_font_units: int, lsb_font_units: int,
) -> Optional[bytes]:
    """Вставить simple glyph в слот dst_cid."""
    dst = TTFont(BytesIO(dst_ff2))
    template = TTFont(BytesIO(dst_ff2))
    _ = dst["glyf"]
    _ = dst["hmtx"]
    _ = dst["maxp"]
    _ = dst["head"]
    upem = int(dst["head"].unitsPerEm) or 2048
    dst_go = list(dst.getGlyphOrder())
    if dst_cid < 0 or dst_cid > len(dst_go):
        return None
    aw_i = int(aw_font_units)
    if aw_i <= 0:
        aw_i = upem // 2
    lsb_i = int(lsb_font_units)
    if dst_cid == len(dst_go):
        new_name = f"glyph{dst_cid:05d}"
        while new_name in dst_go:
            new_name += "x"
        dst_go.append(new_name)
        dst.setGlyphOrder(dst_go)
        dst["glyf"][new_name] = deepcopy(simple)
        dst["hmtx"].metrics[new_name] = (aw_i, lsb_i)
        dst["maxp"].numGlyphs = len(dst_go)
    else:
        name = dst_go[dst_cid]
        dst["glyf"][name] = deepcopy(simple)
        dst["hmtx"].metrics[name] = (aw_i, lsb_i)
    return _save_oracle_ttf(dst, template)


def _pdf_width_from_font_aw(aw_font: int, upem: int) -> int:
    """hmtx font-units → PDF /W (1000-em)."""
    u = upem if upem > 0 else 2048
    return max(1, int(round(aw_font * 1000.0 / u)))


def _font_aw_from_pdf_width(pdf_w: int, upem: int) -> int:
    """PDF /W → hmtx font-units."""
    u = upem if upem > 0 else 2048
    return max(1, int(round(pdf_w * u / 1000.0)))


def inject_alfa_char(
    base_path: str,
    ch: str,
    glyph_donor_path: Optional[str] = None,
) -> Optional[str]:
    """Добавить glyf: сначала PDF-донор Oracle, иначе библиотека (без FontBBox patch)."""
    if not ch or ch in ("\n", "\r", "\t"):
        return base_path
    cp = _char_codepoint(ch)

    ctx_b = AlfaOrigContext()
    if not ctx_b.load(base_path):
        return None
    refs_b = _load_font_xrefs(base_path)
    if not refs_b:
        return None

    with open(base_path, "rb") as f:
        pdf = bytearray(f.read())

    dst_ff2_ttf = _ff2_read_decompressed(bytes(pdf), refs_b["ff2"])
    if _char_glyph_ok(ctx_b, dst_ff2_ttf, ch):
        return base_path

    dst_ft_meta = TTFont(BytesIO(dst_ff2_ttf))
    upem = int(dst_ft_meta["head"].unitsPerEm) or 2048
    del dst_ft_meta

    simple = None
    aw_font = upem // 2
    lsb_font = 0
    pdf_w = 500
    src_label = "library"
    use_copy_slot = False
    src_ff2_ttf = None
    src_cid = None

    if glyph_donor_path:
        ctx_s = AlfaOrigContext()
        refs_s = _load_font_xrefs(glyph_donor_path)
        if ctx_s.load(glyph_donor_path) and refs_s:
            with open(glyph_donor_path, "rb") as donor_file:
                donor_pdf = donor_file.read()
            src_ff2_ttf = _ff2_read_decompressed(donor_pdf, refs_s["ff2"])
            src_cid = ctx_s.uni_to_cid.get(cp)
            if src_cid is not None and _glyph_slot_has_ink(src_ff2_ttf, src_cid):
                src_ft = TTFont(BytesIO(src_ff2_ttf))
                src_name = src_ft.getGlyphOrder()[src_cid]
                simple = _as_simple_glyph(src_ft, src_name)
                aw_font, lsb_font = src_ft["hmtx"].metrics[src_name]
                aw_font = int(aw_font)
                lsb_font = int(lsb_font)
                pdf_w = int(ctx_s.widths.get(src_cid) or _pdf_width_from_font_aw(aw_font, upem))
                src_label = os.path.basename(glyph_donor_path)
                use_copy_slot = True

    if simple is None:
        import alfa_glyph_library as agl

        agl.ensure_library()
        got = agl.get_glyph(cp)
        if got:
            simple, aw_font, lsb_font = got
            aw_font = int(aw_font)
            lsb_font = int(lsb_font)
            if upem != 2048 and upem > 0:
                from alfa_glyph_library import _scale_glyph

                scale = upem / 2048.0
                simple = _scale_glyph(simple, scale)
                aw_font = int(round(aw_font * scale))
                lsb_font = int(round(lsb_font * scale))
            pdf_w = _pdf_width_from_font_aw(aw_font, upem)
            src_label = "library"

    if simple is None or getattr(simple, "numberOfContours", 0) <= 0:
        return None

    stream_used = _collect_alfa_used_cids(bytes(ctx_b.stream))

    dst_cid = ctx_b.uni_to_cid.get(cp)
    need_tu = dst_cid is None
    if dst_cid is not None and dst_cid in _component_cids(dst_ff2_ttf):
        dst_cid = None
        need_tu = True
    if dst_cid is None:
        dst_cid = _alloc_cid(ctx_b, dst_ff2_ttf, stream_used)
    if dst_cid is None:
        return None

    if use_copy_slot and src_ff2_ttf is not None and src_cid is not None:
        new_ff2_ttf = _copy_glyf_slot(dst_ff2_ttf, src_ff2_ttf, src_cid, dst_cid)
    else:
        new_ff2_ttf = _install_simple_glyph(
            dst_ff2_ttf, dst_cid, simple, int(aw_font), int(lsb_font),
        )
    if not new_ff2_ttf or not _glyph_slot_has_ink(new_ff2_ttf, dst_cid):
        return None
    if not _patch_ff2_decompressed(pdf, refs_b["ff2"], new_ff2_ttf):
        return None

    if need_tu:
        tu_dec = _tu_read_decompressed(bytes(pdf), refs_b["tu"])
        tu_new = _append_bfchar_to_tounicode(tu_dec, dst_cid, cp)
        if tu_new is None:
            return None
        if not _patch_tu_decompressed(pdf, refs_b["tu"], tu_new):
            return None
        ctx_b.uni_to_cid[cp] = dst_cid
        ctx_b.cid_to_uni[dst_cid] = cp

    ctx_b.widths[dst_cid] = int(pdf_w)
    if not _append_oracle_cid_width(pdf, refs_b["cid"], dst_cid, int(pdf_w)):
        return None

    ctx_check = AlfaOrigContext()
    if not ctx_check.load_bytes(bytes(pdf)):
        return None
    if not _stream_labels_unchanged(ctx_b, ctx_check):
        logger.warning(
            "Alfa inject %r broke static labels on %s — rollback",
            ch,
            os.path.basename(base_path),
        )
        return None
    ok, miss = _glyphs_ok_for_text(ctx_check, ctx_check.pdf_bytes, ch)
    if not ok:
        logger.warning("Alfa inject verify failed for %r: %s", ch, miss)
        return None

    fd, out = tempfile.mkstemp(suffix=".pdf", prefix="alfa_font_inj_")
    os.close(fd)
    with open(out, "wb") as f:
        f.write(bytes(pdf))
    logger.info(
        "Alfa glyph inject %r cid=%d from %s -> %s",
        ch,
        dst_cid,
        src_label,
        os.path.basename(base_path),
    )
    return out


def _donor_glyph_score(path: str, text: str) -> int:
    ctx = AlfaOrigContext()
    if not ctx.load(path):
        return -1
    need = [c for c in text or "" if c not in ("\n", "\r", "\t")]
    if not need:
        return 0
    ok, miss = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, text)
    return len(need) - len(miss)


def ensure_alfa_font_chars(base_path: str, text: str, pool: List[str]) -> Optional[str]:
    """Гарантировать charset: PDF-донор → библиотека, без swap ToUnicode."""
    import alfa_glyph_library as agl

    agl.ensure_library()

    ctx = AlfaOrigContext()
    if not ctx.load(base_path):
        return None
    ok, miss = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, text)
    if ok:
        return base_path

    candidates = [p for p in pool if p and os.path.isfile(p)]
    if base_path not in candidates:
        candidates.insert(0, base_path)

    working = base_path
    seen: Set[str] = set()
    # Rare glyphs first — late CID slots often fail for й after bulk inject
    rare_first = "йЙёЁъыэщШЩЦЮ"
    miss_ordered = sorted(
        set(miss),
        key=lambda c: (0 if c in rare_first else 1, ord(c)),
    )
    for ch in miss_ordered:
        if ch in seen or ch in ("\n", "\r", "\t"):
            continue
        seen.add(ch)
        ctx = AlfaOrigContext()
        if not ctx.load(working):
            continue
        ok, miss_now = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, ch)
        if ok:
            continue
        donor = _find_donor_for_char(candidates, ch)
        if not donor and not agl.has_char(ch):
            logger.warning("Alfa: no source for %r", ch)
            continue
        injected = inject_alfa_char(working, ch, donor)
        if injected:
            working = injected
        else:
            logger.warning("Alfa: cannot inject %r", ch)

    ctx = AlfaOrigContext()
    if not ctx.load(working):
        return None
    ok, miss = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, text)
    if not ok:
        logger.warning("Alfa font extend incomplete, missing: %s", "".join(miss[:12]))
        return None
    return working
