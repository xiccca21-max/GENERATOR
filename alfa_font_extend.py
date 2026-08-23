"""Расширение subset Tahoma в Alfa PDF — swap/inject как у T-Bank SBP."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import struct
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
    _TM_RE,
)

logger = logging.getLogger(__name__)

# Value slots rewritten per receipt (same y/x as alfa_sbp_stealth.SBP_COORDS).
_ALFA_SBP_VALUE_YX = (
    (779.15, 452.788),
    (664.3, 35.45),
    (621.4, 35.45),
    (578.5, 35.45),
    (535.6, 35.45),
    (492.7, 35.45),
    (664.3, 304.75),
    (621.4, 304.75),
    (578.5, 304.75),
    (535.6, 304.75),
    (492.7, 304.75),
)


def _collect_alfa_used_cids(stream: bytes) -> Set[int]:
    """CID из hex-строк `<....>Tj` в Alfa content stream."""
    used: Set[int] = set()
    for m in re.finditer(rb"<([0-9A-Fa-f]+)>", stream):
        hx = m.group(1)
        for i in range(0, len(hx), 4):
            used.add(int(hx[i : i + 4], 16))
    return used


def _is_value_tm(mx: float, my: float, value_yx: Tuple[Tuple[float, float], ...]) -> bool:
    for y, x in value_yx:
        if abs(my - y) <= 1.5 and abs(mx - x) <= 3.0:
            return True
    return False


def _cids_from_hex(hx: bytes) -> Set[int]:
    out: Set[int] = set()
    for i in range(0, len(hx), 4):
        out.add(int(hx[i : i + 4], 16))
    return out


def _label_cids(
    ctx: AlfaOrigContext,
    value_yx: Tuple[Tuple[float, float], ...] = _ALFA_SBP_VALUE_YX,
) -> Set[int]:
    """CIDs painted on static labels / title — never steal these."""
    keep: Set[int] = set()
    stream = bytes(ctx.stream)
    for m in _TM_RE.finditer(stream):
        mx, my = float(m.group(1)), float(m.group(2))
        if _is_value_tm(mx, my, value_yx):
            continue
        tail = stream[m.end() : m.end() + 220]
        tj = re.search(rb"<([0-9A-Fa-f]+)>", tail)
        if not tj:
            continue
        keep |= _cids_from_hex(tj.group(1))
    return keep


def _pin_head_checksum_adjustment(ttf: bytes, csa: int) -> bytes:
    """Oracle keeps source Tahoma checkSumAdjustment on every subset.

    fontTools.save() recalculates it — HARD ALFA_ORACLE_TTF_HEAD_MECHANICS_CONFLICT
    and the bankpdf reassembly tell.
    """
    if len(ttf) < 28:
        return ttf
    n = struct.unpack(">H", ttf[4:6])[0]
    buf = bytearray(ttf)
    packed = struct.pack(">I", int(csa) & 0xFFFFFFFF)
    for i in range(n):
        rec = 12 + i * 16
        if rec + 16 > len(buf):
            break
        if bytes(buf[rec:rec + 4]) != b"head":
            continue
        off = struct.unpack(">I", buf[rec + 8:rec + 12])[0]
        if off + 12 <= len(buf):
            buf[off + 8:off + 12] = packed
        break
    return bytes(buf)


def _head_table_offset(ttf: bytes) -> Optional[int]:
    if len(ttf) < 28:
        return None
    n = struct.unpack(">H", ttf[4:6])[0]
    for i in range(n):
        rec = 12 + i * 16
        if rec + 16 > len(ttf):
            break
        if ttf[rec:rec + 4] == b"head":
            off = struct.unpack(">I", ttf[rec + 8:rec + 12])[0]
            if off + 12 <= len(ttf):
                return off
            return None
    return None


def _ot_recalc_checksum_adjustment(ttf: bytes) -> bytes:
    """OpenType formula: checkSumAdjustment = 0xB1B0AFBA - sum32(font).

    bankpdf HARD when this does not match (streak_08 adj=1234120242).
    Call after any TTF pad/mutation. Do NOT pin Alfa canon 1757709444.
    """
    off = _head_table_offset(ttf)
    if off is None:
        return ttf
    buf = bytearray(ttf)
    buf[off + 8:off + 12] = b"\x00\x00\x00\x00"
    padded = bytes(buf)
    if len(padded) % 4:
        padded = padded + b"\x00" * (4 - (len(padded) % 4))
    total = 0
    for i in range(0, len(padded), 4):
        total = (total + struct.unpack(">I", padded[i:i + 4])[0]) & 0xFFFFFFFF
    adj = (0xB1B0AFBA - total) & 0xFFFFFFFF
    buf[off + 8:off + 12] = struct.pack(">I", adj)
    return bytes(buf)


def _ot_checksum_matches(ttf: bytes) -> bool:
    off = _head_table_offset(ttf)
    if off is None:
        return False
    got = struct.unpack(">I", ttf[off + 8:off + 12])[0]
    # Oracle-native emit pins corpus CSA; other paths use OpenType formula.
    if got == 1757709444:
        return True
    want = struct.unpack(
        ">I", _ot_recalc_checksum_adjustment(ttf)[off + 8:off + 12],
    )[0]
    return got == want


def uniquify_alfa_ff2_ot_valid(pdf: bytes) -> Optional[bytes]:
    """Unique FontFile2 without touching used outlines.

    Unused hmtx +1 → fontTools save → OpenType CSA. Atlas of painted glyphs
    stays; bankpdf caches exact FF2 bytes so a PASS font cannot be reused.
    """
    import secrets

    refs = _load_font_xrefs_from_bytes(pdf)
    if not refs:
        return None
    ff2 = _ff2_read_decompressed(pdf, refs["ff2"])
    if not ff2:
        return None
    ctx = AlfaOrigContext()
    if not ctx.load_bytes(pdf):
        return None
    used = _collect_alfa_used_cids(bytes(ctx.stream)) | _label_cids(ctx)
    ft = TTFont(BytesIO(ff2))
    template = TTFont(BytesIO(ff2))
    go = ft.getGlyphOrder()
    cands = [i for i in range(1, len(go)) if i not in used]
    if not cands:
        return None
    pick = cands[int.from_bytes(secrets.token_bytes(2), "big") % len(cands)]
    name = go[pick]
    aw, lsb = ft["hmtx"].metrics[name]
    ft["hmtx"].metrics[name] = (int(aw) + 1, int(lsb))
    new_ff2 = _ot_recalc_checksum_adjustment(_save_oracle_ttf(ft, template))
    if not _ot_checksum_matches(new_ff2):
        logger.warning("Alfa uniquify: OT checksum still mismatch")
        return None
    if hashlib.sha256(new_ff2).digest() == hashlib.sha256(ff2).digest():
        return None
    out = bytearray(pdf)
    if not _patch_ff2_decompressed(out, refs["ff2"], new_ff2):
        return None
    landed_refs = _load_font_xrefs_from_bytes(bytes(out)) or refs
    landed = _ff2_read_decompressed(bytes(out), landed_refs["ff2"])
    if not landed or not _ot_checksum_matches(landed):
        logger.warning("Alfa uniquify: landed FF2 OT checksum fail")
        return None
    logger.info(
        "Alfa uniquify unused hmtx cid=%d aw=%d→%d ff2 %s→%s",
        pick, int(aw), int(aw) + 1,
        hashlib.sha256(ff2).hexdigest()[:16],
        hashlib.sha256(landed).hexdigest()[:16],
    )
    return bytes(out)


def _repack_sfnt_split(
    font_bytes: bytes,
    dir_order: list,
    phys_order: list,
    *,
    pad_glyf: bool = False,
) -> bytes:
    """Directory tags in dir_order; payloads laid out in phys_order.

    Offsets are rewritten. Tables stay 4-byte zero-padded except glyf when
    pad_glyf is False: Oracle packs the next table immediately after glyf even
    when len(glyf) % 4 == 2. Directory checksums of table bodies are unchanged.
    """
    import math
    import struct

    data = font_bytes
    if len(data) < 12:
        return font_bytes
    sfnt_version = data[0:4]
    num_tables = struct.unpack(">H", data[4:6])[0]
    tables: dict = {}
    for i in range(num_tables):
        e = 12 + i * 16
        tag_str = data[e:e + 4].decode("latin-1")
        checksum = struct.unpack(">I", data[e + 4:e + 8])[0]
        tbl_off = struct.unpack(">I", data[e + 8:e + 12])[0]
        tbl_len = struct.unpack(">I", data[e + 12:e + 16])[0]
        tables[tag_str] = {
            "checksum": checksum,
            "data": data[tbl_off:tbl_off + tbl_len],
        }

    def _merge(preferred: list) -> list:
        order = [t for t in preferred if t in tables]
        for t in tables:
            if t not in order:
                order.append(t)
        return order

    dir_tags = _merge(dir_order)
    phys_tags = _merge(phys_order)
    n = len(dir_tags)
    entry_selector = int(math.log2(n)) if n > 0 else 0
    search_range = (2 ** entry_selector) * 16
    range_shift = n * 16 - search_range
    header = sfnt_version + struct.pack(
        ">HHHH", n, search_range, entry_selector, range_shift,
    )

    data_start = 12 + n * 16
    current_off = data_start
    phys_info = {}
    body = b""
    for tag_str in phys_tags:
        tbl = tables[tag_str]
        tbl_bytes = tbl["data"]
        if tag_str == "glyf" and not pad_glyf:
            pad = b""
        else:
            pad = b"\x00" * ((4 - (len(tbl_bytes) % 4)) % 4)
        phys_info[tag_str] = (current_off, len(tbl_bytes), tbl["checksum"])
        body += tbl_bytes + pad
        current_off += len(tbl_bytes) + len(pad)

    directory = b""
    for tag_str in dir_tags:
        off, length, checksum = phys_info[tag_str]
        directory += tag_str.encode("latin-1") + struct.pack(">III", checksum, off, length)
    return header + directory + body


def _ot_table_checksum(table_bytes: bytes) -> int:
    padded = table_bytes + b"\x00" * ((-len(table_bytes)) % 4)
    total = 0
    for i in range(0, len(padded), 4):
        total = (total + struct.unpack(">I", padded[i:i + 4])[0]) & 0xFFFFFFFF
    return total


def _finalize_sfnt_checksums(ttf: bytes) -> bytes:
    """Rewrite directory table checksums (head CSA=0), then pin Oracle CSA.

    Does not apply the textbook whole-SFNT formula.
    """
    if len(ttf) < 12:
        return ttf
    n = struct.unpack(">H", ttf[4:6])[0]
    buf = bytearray(ttf)
    for i in range(n):
        e = 12 + i * 16
        tag = bytes(buf[e:e + 4])
        off = struct.unpack(">I", buf[e + 8:e + 12])[0]
        ln = struct.unpack(">I", buf[e + 12:e + 16])[0]
        body = bytes(buf[off:off + ln])
        if tag == b"head" and len(body) >= 12:
            body = bytearray(body)
            body[8:12] = b"\x00\x00\x00\x00"
            body = bytes(body)
        buf[e + 4:e + 8] = struct.pack(">I", _ot_table_checksum(body))
    return _pin_head_checksum_adjustment(bytes(buf), 1757709444)


def _sfnt_physical_tags(ttf: bytes) -> list:
    n = struct.unpack(">H", ttf[4:6])[0]
    recs = []
    for i in range(n):
        rec = 12 + i * 16
        tag = ttf[rec:rec + 4].decode("latin-1")
        off = struct.unpack(">I", ttf[rec + 8:rec + 12])[0]
        recs.append((off, i, tag))
    recs.sort()
    return [t for _off, _i, t in recs]


def _save_oracle_ttf(
    font: TTFont,
    template: Optional[TTFont] = None,
    *,
    oracle_native: bool = False,
    competitor_repack: bool = False,
    keep_cmap: bool = False,
) -> bytes:
    """fontTools.save; preserve Oracle head flags + Alfa created/modified epochs.

    fontTools defaults break Proton HARD checks:
      - recalcTimestamp=True → head.modified becomes \"now\"
      - loca.compile → short format (indexToLocFormat=0) when glyf is small
      - recalcBBoxes=True → head xMin.. drift vs frozen /FontBBox
    Oracle Alfa F1 requires flags=27, indexToLocFormat=1, and
    created=2931866140 ⇒ modified=3170332026.

    oracle_native=True: directory and payloads in Oracle order
    cvt,fpgm,glyf,head,hhea,hmtx,loca,maxp,prep; pin CSA 1757709444.
    Each glyf record is padded to even length (Oracle loca); maxp/hhea
    capacity fields are copied from the live shell, not recalculated.
    competitor_repack=True: head,hhea,maxp,hmtx,fpgm,prep,cvt,loca,glyf
    + textbook checkSumAdjustment. Native path is unchanged.
    """
    import array
    import sys as _sys

    _ALFA_CREATED = 2931866140
    _ALFA_MODIFIED = 3170332026
    _ORACLE_DIR_ORDER = [
        "cvt ", "fpgm", "glyf", "head", "hhea", "hmtx", "loca", "maxp", "prep",
    ]
    _COMPETITOR_PHYS = [
        "head", "hhea", "maxp", "hmtx", "fpgm", "prep", "cvt ", "loca", "glyf",
    ]
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
        if oracle_native and "maxp" in template and "maxp" in font:
            num = int(font["maxp"].numGlyphs)
            for attr in (
                "maxPoints", "maxContours", "maxCompositePoints",
                "maxCompositeContours", "maxZones", "maxTwilightPoints",
                "maxStorage", "maxFunctionDefs", "maxInstructionDefs",
                "maxStackElements", "maxSizeOfInstructions",
                "maxComponentElements", "maxComponentDepth",
            ):
                if hasattr(template["maxp"], attr):
                    setattr(font["maxp"], attr, int(getattr(template["maxp"], attr)))
            font["maxp"].numGlyphs = num
        if oracle_native and "hhea" in template and "hhea" in font:
            for attr in (
                "ascent", "descent", "lineGap", "advanceWidthMax",
                "minLeftSideBearing", "minRightSideBearing", "xMaxExtent",
                "caretSlopeRise", "caretSlopeRun", "caretOffset",
            ):
                if hasattr(template["hhea"], attr):
                    setattr(font["hhea"], attr, int(getattr(template["hhea"], attr)))
    created = int(getattr(font["head"], "created", 0) or 0)
    if created == _ALFA_CREATED:
        font["head"].created = _ALFA_CREATED
        font["head"].modified = _ALFA_MODIFIED
    font["head"].indexToLocFormat = 1
    if (oracle_native or competitor_repack) and "hhea" in font and "maxp" in font:
        font["hhea"].numberOfHMetrics = int(font["maxp"].numGlyphs)

    if oracle_native:
        order = [t for t in _ORACLE_DIR_ORDER if t in font]
        if keep_cmap and "cmap" in font and "cmap" not in order:
            order.append("cmap")
        for tag in list(font.keys()):
            if tag not in order and tag not in ("GlyphOrder",):
                if tag == "cmap" and keep_cmap:
                    continue
                if tag not in _ORACLE_DIR_ORDER:
                    try:
                        del font[tag]
                    except Exception:
                        pass
        font.tableOrder = [t for t in order if t in font]
    elif competitor_repack:
        for tag in list(font.keys()):
            if tag not in _COMPETITOR_PHYS and tag not in ("GlyphOrder",):
                try:
                    del font[tag]
                except Exception:
                    pass
        font.tableOrder = [t for t in _COMPETITOR_PHYS if t in font]

    # Force long loca (Oracle indexToLocFormat=1) — default compile picks short.
    loca = font["loca"]
    _orig_compile = loca.compile
    hmtx = font["hmtx"]
    _orig_hmtx = hmtx.compile

    def _compile_long(ttFont):
        try:
            locations = array.array("I", loca.locations)
        except AttributeError:
            loca.set([])
            locations = array.array("I", loca.locations)
        ttFont["head"].indexToLocFormat = 1
        if _sys.byteorder != "big":
            locations.byteswap()
        return locations.tobytes()

    def _compile_full_hmtx(ttFont):
        metrics = hmtx.metrics
        chunks = []
        for name in ttFont.getGlyphOrder():
            aw, lsb = metrics[name]
            chunks.append(struct.pack(">Hh", int(aw) & 0xFFFF, int(lsb)))
        return b"".join(chunks)

    loca.compile = _compile_long  # type: ignore[method-assign]
    hmtx.compile = _compile_full_hmtx  # type: ignore[method-assign]
    _glyph_compile = None
    if oracle_native:
        from fontTools.ttLib.tables._g_l_y_f import Glyph as _Glyph

        _glyph_compile = _Glyph.compile

        def _compile_even(self, *args, **kwargs):
            data = _glyph_compile(self, *args, **kwargs)
            if data and len(data) % 2:
                data += b"\x00"
            return data

        _Glyph.compile = _compile_even  # type: ignore[method-assign]
    try:
        out = BytesIO()
        font.save(out)
        raw = out.getvalue()
        if oracle_native:
            raw = _repack_sfnt_split(
                raw, list(font.tableOrder), list(font.tableOrder),
                pad_glyf=False,
            )
            return _finalize_sfnt_checksums(raw)
        if competitor_repack:
            raw = _repack_sfnt_split(
                raw, list(_COMPETITOR_PHYS), list(_COMPETITOR_PHYS),
                pad_glyf=True,
            )
            return _ot_recalc_checksum_adjustment(raw)
        return _ot_recalc_checksum_adjustment(raw)
    finally:
        loca.compile = _orig_compile  # type: ignore[method-assign]
        hmtx.compile = _orig_hmtx  # type: ignore[method-assign]
        if _glyph_compile is not None:
            from fontTools.ttLib.tables._g_l_y_f import Glyph as _Glyph

            _Glyph.compile = _glyph_compile  # type: ignore[method-assign]


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


def _neutralize_orphan_tounicode(
    tu_bytes: bytes,
    keep: Set[int],
    only_cids: Optional[Set[int]] = None,
) -> bytes:
    """Unused printable CIDs → U+0000. Live @bankpdfbot PASS zeros exactly CID 42 and 44."""
    text = tu_bytes.decode("latin1", "replace")
    pairs = list(re.finditer(r"<([0-9A-Fa-f]{4})>[ \t]+<([0-9A-Fa-f]{4})>", text))
    if not pairs:
        return tu_bytes
    out = text
    for m in reversed(pairs):
        cid = int(m.group(1), 16)
        if cid in keep or cid == 0:
            continue
        if only_cids is not None and cid not in only_cids:
            continue
        uni_hex = m.group(2)
        try:
            cp = int(uni_hex, 16)
        except ValueError:
            continue
        if cp <= 0x20 or cp == 0xA0:
            continue
        zero = "0" * len(uni_hex)
        start, end = m.start(2), m.end(2)
        out = out[:start] + zero + out[end:]
    return out.encode("latin1") if out != text else tu_bytes


# pdf (3) / five_01+five_02: only these unused CIDs are U+0000 (й and Ч).
# Zeroing CID 43 (В) when the bank is not ВТБ made five_04 FAIL (zeros=3).
_ALFA_PASS_ZERO_CIDS = frozenset({42, 44})


def _closure_fix_alfa_font(
    pdf: bytearray,
    stream: bytes,
    *,
    only_cids: Optional[Set[int]] = _ALFA_PASS_ZERO_CIDS,
) -> bytes:
    """U+0000 on unused printable CIDs. 014153 PASS zeroed all orphans (only_cids=None)."""
    active = _collect_alfa_used_cids(stream)
    if not active:
        return bytes(pdf)
    keep = set(active)
    keep.add(0)
    refs = _load_font_xrefs_from_bytes(bytes(pdf))
    if not refs:
        return bytes(pdf)
    tu_dec = _tu_read_decompressed(bytes(pdf), refs["tu"])
    tu_new = _neutralize_orphan_tounicode(tu_dec, keep, only_cids=only_cids)
    if tu_new != tu_dec:
        if not _patch_tu_decompressed(pdf, refs["tu"], tu_new):
            logger.warning("Alfa closure-fix ToUnicode patch failed")
            return bytes(pdf)
        logger.info(
            "Alfa closure-fix: neutralized orphans keep=%d pin=%s",
            len(active),
            "all" if only_cids is None else sorted(only_cids),
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
    orig_len = ce - cs
    level = _zlib_level_from_header(bytes(pdf)[cs : cs + 2])
    # Как у Oracle: не zlib.compress (иначе MIXED_ZLIB с image-потоками).
    compressed = _unreproducible_flate(new_tu, level)
    if len(compressed) == orig_len:
        pdf[cs:ce] = compressed
        return True
    patched = _patch_length_and_rebuild(pdf, cs, ce, compressed)
    if patched is None:
        logger.warning("Alfa ToUnicode: exact flate miss — skip xref rebuild")
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


def _ff2_java_exact(ttf: bytes, orig_len: int, level: int) -> Optional[Tuple[bytes, bytes]]:
    """Use the natural SFNT only; never append bytes to match donor /Length."""
    from alfa_orig_mode import _oracle_near_flate

    ttf_csa = _ot_recalc_checksum_adjustment(ttf)
    comp = _oracle_near_flate(ttf_csa, level)
    if len(comp) == orig_len:
        return comp, ttf_csa
    return None


def _patch_ff2_decompressed(pdf: bytearray, ff2_xref: int, new_ttf: bytes) -> bool:
    from alfa_emit import sfnt_has_exact_aligned_end

    if not sfnt_has_exact_aligned_end(new_ttf):
        logger.warning("Alfa FF2: bytes found after aligned final SFNT table")
        return False
    pos = _find_stream_pos_for_xref(bytes(pdf), ff2_xref)
    if not pos:
        return False
    cs, ce = pos
    level = _zlib_level_from_header(bytes(pdf)[cs : cs + 2])
    orig_len = ce - cs
    hit = _ff2_java_exact(new_ttf, orig_len, level)
    if hit:
        compressed, used_ttf = hit
        pdf[cs:ce] = compressed
        return _patch_fontfile2_length1(pdf, ff2_xref, len(used_ttf))
    from alfa_orig_mode import _unreproducible_flate

    compressed = _unreproducible_flate(new_ttf, level)
    if len(compressed) == orig_len:
        pdf[cs:ce] = compressed
        return _patch_fontfile2_length1(pdf, ff2_xref, len(new_ttf))
    patched = _patch_length_and_rebuild(pdf, cs, ce, compressed)
    if patched is None:
        logger.warning("Alfa FF2: cannot land new subset without broken xref")
        return False
    pdf[:] = patched
    return _patch_fontfile2_length1(pdf, ff2_xref, len(new_ttf))


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
        if "unlocked" in os.path.basename(path).lower():
            continue
        ctx = AlfaOrigContext()
        if not ctx.load(path):
            continue
        ok, _ = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, ch)
        if ok:
            return path
    return None


def _stream_labels_unchanged(before: AlfaOrigContext, after: AlfaOrigContext) -> bool:
    """Подписи (не value-слоты) должны декодироваться так же после inject."""
    stream = bytes(before.stream)
    for m in _TM_RE.finditer(stream):
        mx, my = float(m.group(1)), float(m.group(2))
        if _is_value_tm(mx, my, _ALFA_SBP_VALUE_YX):
            continue
        tail = stream[m.end() : m.end() + 220]
        tj = re.search(rb"<([0-9A-Fa-f]+)>", tail)
        if not tj:
            continue
        hx = tj.group(1)
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
    if re.search(rf"<{cid:04X}>[ \t]+<{cp:04X}>", text):
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


def _set_bfchar_unicode(tu_bytes: bytes, cid: int, cp: int) -> Optional[bytes]:
    """Rewrite existing CID→Unicode on the same line. Do not grow bfchar or write U+0000."""
    text = tu_bytes.decode("latin1", "replace")
    pat = re.compile(rf"<({cid:04X})>[ \t]+<([0-9A-Fa-f]+)>")
    matches = list(pat.finditer(text))
    if not matches:
        return _append_bfchar_to_tounicode(tu_bytes, cid, cp)
    newu = f"{cp:04X}"
    out = text
    for m in reversed(matches):
        old = m.group(2)
        repl = newu if len(newu) >= len(old) else newu.rjust(len(old), "0")
        out = out[: m.start(2)] + repl + out[m.end(2) :]
    return out.encode("latin1")


def _rebind_bfchar_inplace(
    tu_bytes: bytes, old_cid: int, new_cid: int, new_cp: int,
) -> Optional[bytes]:
    """Same-length CMap rewrite: <old> <uni> → <new> <uni'>. No beginbfchar grow."""
    text = tu_bytes.decode("latin1", "replace")
    pat = re.compile(rf"<({old_cid:04X})>([ \t]+)<([0-9A-Fa-f]+)>")
    m = pat.search(text)
    if not m:
        return None
    old_uni = m.group(3)
    new_uni = f"{new_cp:04X}"
    if len(new_uni) != len(old_uni):
        new_uni = new_uni.rjust(len(old_uni), "0")[-len(old_uni):]
    repl = f"<{new_cid:04X}>{m.group(2)}<{new_uni}>"
    if len(repl) != (m.end() - m.start()):
        return None
    out = text[: m.start()] + repl + text[m.end() :]
    return out.encode("latin1")


def _sfnt_table_off(ttf: bytes, tag: bytes) -> Optional[int]:
    if len(ttf) < 12:
        return None
    n = struct.unpack(">H", ttf[4:6])[0]
    for i in range(n):
        rec = 12 + i * 16
        if rec + 16 > len(ttf):
            break
        if ttf[rec:rec + 4] == tag:
            return struct.unpack(">I", ttf[rec + 8:rec + 12])[0]
    return None


def _inject_glyf_raw_inplace(
    ttf: bytes, dst_cid: int, simple, aw: int, lsb: int,
) -> Optional[bytes]:
    """Overwrite an existing glyf slot. Same TTF length — no fontTools.save."""
    ft = TTFont(BytesIO(ttf))
    go = list(ft.getGlyphOrder())
    if dst_cid < 0 or dst_cid >= len(go):
        return None
    try:
        from fontTools.ttLib.tables.ttProgram import Program

        prog = Program()
        prog.fromBytecode(b"")
        simple.program = prog
        payload = simple.compile(ft)
    except Exception:
        return None
    locs = list(ft["loca"].locations)
    slot = locs[dst_cid + 1] - locs[dst_cid]
    if len(payload) > slot:
        return None
    glyf_off = _sfnt_table_off(ttf, b"glyf")
    hmtx_off = _sfnt_table_off(ttf, b"hmtx")
    if glyf_off is None or hmtx_off is None:
        return None
    buf = bytearray(ttf)
    start = glyf_off + locs[dst_cid]
    buf[start:start + slot] = payload + (b"\x00" * (slot - len(payload)))
    lsb_i = max(-32767, min(32767, int(lsb)))
    aw_i = max(0, int(aw)) & 0xFFFF
    hm = hmtx_off + dst_cid * 4
    if hm + 4 <= len(buf):
        buf[hm:hm + 4] = struct.pack(">Hh", aw_i, lsb_i)
    return bytes(buf)


def _largest_unused_glyf_cid(
    ctx: AlfaOrigContext, ttf: bytes, need_bytes: int, reserved: Set[int],
) -> Optional[int]:
    ft = TTFont(BytesIO(ttf))
    locs = list(ft["loca"].locations)
    best = None
    best_sz = 10**9
    for cid in range(1, len(locs) - 1):
        if cid in reserved:
            continue
        sz = locs[cid + 1] - locs[cid]
        if sz >= need_bytes and sz < best_sz:
            best = cid
            best_sz = sz
    return best


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


def append_alfa_orphan_clone(base_path: str, src_cid: int = 62) -> Optional[str]:
    """maxp+1: duplicate an already-present unused glyf. No ToUnicode change.

    Unique FontFile2 without permuting unused slots: clone orphan CID 62–76.
    """
    refs = _load_font_xrefs(base_path)
    if not refs:
        return None
    with open(base_path, "rb") as fh:
        pdf = bytearray(fh.read())
    ff2 = _ff2_read_decompressed(bytes(pdf), refs["ff2"])
    ft = TTFont(BytesIO(ff2))
    n = len(ft.getGlyphOrder())
    if src_cid < 1 or src_cid >= n or n >= 220:
        return None
    new_ff2 = _copy_glyf_slot(ff2, ff2, src_cid, n)
    if not new_ff2:
        return None
    if not _patch_ff2_decompressed(pdf, refs["ff2"], new_ff2):
        return None
    fd, out = tempfile.mkstemp(suffix=".pdf", prefix="alfa_font_orph_")
    os.close(fd)
    with open(out, "wb") as fh:
        fh.write(bytes(pdf))
    logger.info("Alfa orphan clone cid=%d -> cid=%d n=%d", src_cid, n, n + 1)
    return out


def _alloc_cid(
    ctx: AlfaOrigContext,
    ff2: bytes,
    stream_used: Optional[Set[int]] = None,
    *,
    seed: bytes = b"",
    extra_reserved: Optional[Set[int]] = None,
    need_cps: Optional[Set[int]] = None,
    label_cids: Optional[Set[int]] = None,
    prefer_append: bool = False,
) -> Optional[int]:
    """Свободный CID без роста maxp: unused ink (донорское ФИО) → empty.

    Не резервируем всю CMap донора — иначе charset = «буквы одного PDF».
    Нельзя трогать: .notdef, composite-deps, статичные подписи, буквы текущего лица.
    five_01/02 live PASS: append maxp+1 (CID 77 on pdf3), do not steal.
    """
    ft = TTFont(BytesIO(ff2))
    n = len(ft.getGlyphOrder())
    if prefer_append and n < 220:
        return n
    reserved = {0}
    reserved |= set(label_cids or ())
    reserved |= set(extra_reserved or ())
    reserved |= set(stream_used or ())
    reserved |= _component_cids(ff2)
    need = set(need_cps or ())
    for cp, cid in ctx.uni_to_cid.items():
        if cp in need:
            reserved.add(cid)

    glyf = ft["glyf"]
    go = ft.getGlyphOrder()

    def _pick(cids: List[int]) -> Optional[int]:
        if not cids:
            return None
        if seed:
            return cids[int(hashlib.sha256(seed).hexdigest(), 16) % len(cids)]
        return cids[0]

    steal_tu: List[int] = []
    steal_other: List[int] = []
    empties: List[int] = []
    for cid in range(n):
        if cid in reserved:
            continue
        nc = int(getattr(glyf[go[cid]], "numberOfContours", 0) or 0)
        if nc == 0:
            empties.append(cid)
            continue
        if cid in ctx.cid_to_uni:
            steal_tu.append(cid)
        else:
            steal_other.append(cid)
    # Сначала overwrite существующего bfchar (ToUnicode не растёт).
    hit = _pick(steal_tu) or _pick(steal_other) or _pick(empties)
    if hit is not None:
        return hit
    # Последний резерв: новый CID (maxp+1). Без этого полный алфавит не влезает
    # в subset ~70 глифов (подписи + лицо).
    if n < 220:
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


def _set_oracle_cid_width(pdf: bytearray, cid_xref: int, cid: int, width: int) -> bool:
    """Update /W for cid in place. Do not pretty-reprint the whole array."""
    from tbank_orig_mode import find_object_range
    from tbank_sbp_stealth import _replace_byte_range_and_rebuild

    obj_rng = find_object_range(bytes(pdf), cid_xref)
    if not obj_rng:
        return False
    o_start, o_end = obj_rng
    raw_obj = bytes(pdf[o_start:o_end]).decode("latin1", errors="replace")
    widths = _parse_widths(raw_obj)
    target_w = int(width)
    if widths.get(cid) == target_w:
        return True
    if cid not in widths:
        return _append_oracle_cid_width(pdf, cid_xref, cid, target_w)

    br = _oracle_w_bracket(raw_obj)
    if not br:
        return False
    b0, b1 = br
    body = raw_obj[b0:b1]
    token_re = re.compile(r"-?\d+\.?\d*|\[|\]")
    tokens = list(token_re.finditer(body))
    span = None
    j = 0
    ntok = len(tokens)

    def _tok(i: int) -> str:
        return tokens[i].group(0)

    while j < ntok:
        t = _tok(j)
        if t in ("[", "]"):
            j += 1
            continue
        try:
            gid = int(float(t))
        except ValueError:
            j += 1
            continue
        j += 1
        if j < ntok and _tok(j) == "[":
            j += 1
            k = 0
            while j < ntok and _tok(j) != "]":
                try:
                    int(float(_tok(j)))
                except ValueError:
                    j += 1
                    continue
                if gid + k == cid:
                    span = tokens[j].span()
                k += 1
                j += 1
            if j < ntok and _tok(j) == "]":
                j += 1
        elif j + 1 < ntok:
            try:
                gid_hi = int(float(_tok(j)))
                w_idx = j + 1
                int(float(_tok(w_idx)))
                if gid <= cid <= gid_hi and gid == gid_hi:
                    span = tokens[w_idx].span()
                j += 2
            except ValueError:
                j += 1
    if span is None:
        return _append_oracle_cid_width(pdf, cid_xref, cid, target_w)
    new_body = body[: span[0]] + str(target_w) + body[span[1] :]
    new_obj = raw_obj[:b0] + new_body + raw_obj[b1:]
    new_obj_b = new_obj.encode("latin1")
    if len(new_obj_b) == o_end - o_start:
        pdf[o_start:o_end] = new_obj_b
        return True
    from tbank_sbp_stealth import _replace_byte_range_and_rebuild

    patched = _replace_byte_range_and_rebuild(bytes(pdf), o_start, o_end, new_obj_b)
    if patched is None:
        logger.warning("Alfa /W: length drift")
        return False
    pdf[:] = patched
    return True


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
        logger.warning("Alfa /W append: length drift")
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
    *,
    face_text: str = "",
    prefer_append: bool = False,
) -> Optional[str]:
    """Поставить glyf в чужой/пустой CID донора.

    prefer_append=True: five_01/02 recipe — new CID at maxp (pdf3 → 77).
    """
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
        # Do not paint Windows Tahoma into a used CID — bankpdf atlases it.
        if not agl.has_corpus_outline(ch) and ch not in "Жж":
            logger.warning("Alfa inject %r refused: not a live Oracle outline", ch)
            return None
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

    label_cids = _label_cids(ctx_b)
    extra_reserved: Set[int] = set(label_cids)
    need_cps = _needed_codepoints(face_text or ch)
    for face_ch in face_text or "":
        if face_ch in ("\n", "\r", "\t"):
            continue
        fcp = _char_codepoint(face_ch)
        mapped = ctx_b.uni_to_cid.get(fcp)
        if mapped is not None and _glyph_slot_has_ink(dst_ff2_ttf, mapped):
            extra_reserved.add(mapped)

    stream_used = _collect_alfa_used_cids(bytes(ctx_b.stream))
    reserved = {0} | extra_reserved | set(label_cids) | _component_cids(dst_ff2_ttf)
    for cid in stream_used:
        uni = ctx_b.cid_to_uni.get(cid)
        if uni is not None and uni in need_cps:
            reserved.add(cid)
    new_ff2_ttf = None
    dst_cid: Optional[int] = None
    # five_01/02: append maxp with library hints intact. Inplace steal +
    # hint-strip was an experiment and live-failed.
    if not prefer_append:
        try:
            from fontTools.ttLib.tables.ttProgram import Program

            stripped = deepcopy(simple)
            prog = Program()
            prog.fromBytecode(b"")
            stripped.program = prog
            need_bytes = len(stripped.compile(TTFont(BytesIO(dst_ff2_ttf))))
        except Exception:
            stripped = simple
            need_bytes = 10**9
        inplace_cid = _largest_unused_glyf_cid(ctx_b, dst_ff2_ttf, need_bytes, reserved)
        if inplace_cid is not None:
            new_ff2_ttf = _inject_glyf_raw_inplace(
                dst_ff2_ttf, inplace_cid, stripped, int(aw_font), int(lsb_font),
            )
            if new_ff2_ttf and _glyph_slot_has_ink(new_ff2_ttf, inplace_cid):
                dst_cid = inplace_cid
                src_label = f"{src_label}+inplace@{inplace_cid}"

    if dst_cid is None:
        dst_cid = ctx_b.uni_to_cid.get(cp)
        if dst_cid is not None and dst_cid in _component_cids(dst_ff2_ttf):
            dst_cid = None
        if dst_cid is None:
            seed = f"{face_text}|{ch}".encode("utf-8")
            dst_cid = _alloc_cid(
                ctx_b, dst_ff2_ttf, None,
                seed=seed, extra_reserved=extra_reserved,
                need_cps=need_cps, label_cids=label_cids,
                prefer_append=prefer_append,
            )
        if dst_cid is None:
            logger.warning("Alfa inject %r: no stealable CID", ch)
            return None
        go_n = len(TTFont(BytesIO(dst_ff2_ttf)).getGlyphOrder())
        if dst_cid > go_n:
            logger.warning("Alfa inject %r refused cid=%d n=%d", ch, dst_cid, go_n)
            return None
        if use_copy_slot and src_ff2_ttf is not None and src_cid is not None:
            new_ff2_ttf = _copy_glyf_slot(dst_ff2_ttf, src_ff2_ttf, src_cid, dst_cid)
        else:
            new_ff2_ttf = _install_simple_glyph(
                dst_ff2_ttf, dst_cid, simple, int(aw_font), int(lsb_font),
            )
    if not new_ff2_ttf or dst_cid is None or not _glyph_slot_has_ink(new_ff2_ttf, dst_cid):
        return None
    if not _patch_ff2_decompressed(pdf, refs_b["ff2"], new_ff2_ttf):
        return None

    tu_dec = _tu_read_decompressed(bytes(pdf), refs_b["tu"])
    tu_new = _set_bfchar_unicode(tu_dec, dst_cid, cp)
    if not prefer_append and (
        tu_new is None or (tu_new != tu_dec and len(tu_new) != len(tu_dec))
    ):
        tu_new = None
        for old_cid, old_cp in list(ctx_b.cid_to_uni.items()):
            if old_cid in label_cids or old_cid == 0:
                continue
            if old_cp in need_cps:
                continue
            rebound = _rebind_bfchar_inplace(tu_dec, old_cid, dst_cid, cp)
            if rebound is not None and len(rebound) == len(tu_dec):
                tu_new = rebound
                ctx_b.uni_to_cid.pop(old_cp, None)
                ctx_b.cid_to_uni.pop(old_cid, None)
                break
    if tu_new is None:
        return None
    if tu_new != tu_dec and not _patch_tu_decompressed(pdf, refs_b["tu"], tu_new):
        return None
    old_cp = ctx_b.cid_to_uni.get(dst_cid)
    if old_cp is not None and old_cp != cp:
        ctx_b.uni_to_cid.pop(old_cp, None)
    ctx_b.uni_to_cid[cp] = dst_cid
    ctx_b.cid_to_uni[dst_cid] = cp

    ctx_b.widths[dst_cid] = int(pdf_w)
    if not _set_oracle_cid_width(pdf, refs_b["cid"], dst_cid, int(pdf_w)):
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
        mapped = ctx_check.uni_to_cid.get(cp)
        logger.warning(
            "Alfa inject verify failed for %r: %s dst=%d mapped=%s tu=%s w=%s",
            ch, miss, dst_cid, mapped,
            hex(ctx_check.cid_to_uni.get(dst_cid, 0)),
            ctx_check.widths.get(dst_cid),
        )
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


def _tu_zero_count(pdf: bytes) -> int:
    refs = _load_font_xrefs_from_bytes(pdf)
    if not refs:
        return -1
    try:
        tu = _tu_read_decompressed(pdf, refs["tu"])
    except Exception:
        return -1
    text = tu.decode("latin1", "replace")
    n = 0
    for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", text):
        if int(m.group(2), 16) == 0:
            n += 1
    return n


def uniquify_alfa_unused_slots(pdf: bytes, seed: bytes) -> Optional[bytes]:
    """Permute unused glyf slots so FontFile2 SHA is unique per face.

    Glyph count, CID max and ToUnicode stay as in the donor. No PADD / tail pad.
    """
    refs = _load_font_xrefs_from_bytes(pdf)
    if not refs:
        return None
    ctx = AlfaOrigContext()
    if not ctx.load_bytes(pdf):
        return None
    used = _collect_alfa_used_cids(bytes(ctx.stream))
    ff2 = _ff2_read_decompressed(pdf, refs["ff2"])
    components = _component_cids(ff2)
    ft = TTFont(BytesIO(ff2))
    template = TTFont(BytesIO(ff2))
    go = list(ft.getGlyphOrder())
    unused = [
        cid for cid in range(1, len(go))
        if cid not in used and cid not in components
    ]
    glyf = ft["glyf"]
    hmtx = ft["hmtx"]
    if len(unused) >= 2:
        k = 1 + (int(hashlib.sha256(seed).hexdigest(), 16) % (len(unused) - 1))
        names = [go[cid] for cid in unused]
        gcopy = [deepcopy(glyf[nm]) for nm in names]
        mcopy = [hmtx.metrics[nm] for nm in names]
        for i, nm in enumerate(names):
            src = (i - k) % len(names)
            glyf[nm] = gcopy[src]
            hmtx.metrics[nm] = mcopy[src]
    else:
        name0 = go[0]
        aw, lsb = hmtx.metrics[name0]
        delta = 1 + (int(hashlib.sha256(seed).hexdigest(), 16) % 5)
        hmtx.metrics[name0] = (int(aw), int(lsb) + delta)
    new_ttf = _save_oracle_ttf(ft, template)
    buf = bytearray(pdf)
    if not _patch_ff2_decompressed(buf, refs["ff2"], new_ttf):
        return None
    return bytes(buf)


def _donor_glyph_score(path: str, text: str) -> int:
    ctx = AlfaOrigContext()
    if not ctx.load(path):
        return -1
    need = [c for c in text or "" if c not in ("\n", "\r", "\t")]
    if not need:
        return 0
    ok, miss = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, text)
    return len(need) - len(miss)


def ensure_alfa_font_chars(
    base_path: str,
    text: str,
    pool: List[str],
    *,
    prefer_append: bool = False,
) -> Optional[str]:
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
    rare_first = "йЙёЁъыэщШЩЦЮЖжФфХхЧч"
    miss_ordered = sorted(
        set(miss),
        key=lambda c: (0 if c in rare_first else 1, ord(c)),
    )
    for _pass in range(2):
        pending = []
        for ch in miss_ordered:
            if ch in seen or ch in ("\n", "\r", "\t"):
                continue
            ctx = AlfaOrigContext()
            if not ctx.load(working):
                continue
            ok, miss_now = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, ch)
            if ok:
                seen.add(ch)
                continue
            # Ж: library only (streak_03). Never steal Ж from unlocked/other PDF.
            # Other missing letters: do not copy from a second PDF onto this
            # shell (streak_10 Е/г from pdf2 → FAIL). Skip the shell instead.
            if ch in "Жж":
                donor = None
            else:
                donor = _find_donor_for_char(candidates, ch)
                if donor is None:
                    try:
                        from alfa_corpus import canonical_paths

                        extra = []
                        for kind in ("sbp", "card", "phone"):
                            extra.extend(canonical_paths(kind) or [])
                        donor = _find_donor_for_char(extra, ch)
                    except Exception:
                        donor = None
                if donor is not None:
                    same = os.path.normcase(os.path.abspath(donor)) == os.path.normcase(
                        os.path.abspath(working)
                    )
                    if not same:
                        logger.warning(
                            "Alfa: refuse copy %r from %s onto %s",
                            ch, os.path.basename(donor), os.path.basename(working),
                        )
                        pending.append(ch)
                        continue
            if donor is None and not agl.has_corpus_outline(ch) and ch not in "Жж":
                logger.warning("Alfa: no live Oracle outline for %r — skip Windows Tahoma", ch)
                pending.append(ch)
                continue
            if donor is None and not agl.has_char(ch) and ch not in "Жж":
                logger.warning("Alfa: no source for %r", ch)
                pending.append(ch)
                continue
            injected = inject_alfa_char(
                working, ch, donor, face_text=text, prefer_append=prefer_append,
            )
            if injected:
                working = injected
                seen.add(ch)
            else:
                logger.warning("Alfa: cannot inject %r", ch)
                pending.append(ch)
        if not pending:
            break
        miss_ordered = pending

    ctx = AlfaOrigContext()
    if not ctx.load(working):
        return None
    ok, miss = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, text)
    if not ok:
        logger.warning("Alfa font extend incomplete, missing: %s", "".join(miss[:12]))
        return None
    return working
