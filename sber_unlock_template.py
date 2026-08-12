"""
Offline Sber SBP unlocked shell: полный charset из glyph library, xref byte-pipeline.

Run: python build_sber_library.py
Output: templates/S_sbp_unlocked.pdf, templates/S_sbp_runtime.pdf
"""
from __future__ import annotations

import logging
import os
import re
from io import BytesIO
from typing import Dict, Optional, Set

import fitz
from fontTools.ttLib import TTFont

import sber_glyph_library as sgl
import tbank_unlock_template as tut

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
CORPUS_RECEIPT_SHELL = os.path.join(
    os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "сбер", "receipt.pdf",
)
SHELL = os.path.join(_DIR, "templates", "S_sbp_original.pdf")
UNLOCKED = os.path.join(_DIR, "templates", "S_sbp_unlocked.pdf")
RUNTIME = os.path.join(_DIR, "templates", "S_sbp_runtime.pdf")


def _offline_shell_path() -> str:
    """Offline-only: receipt.pdf из корпуса (макс. слоты суммы/банка, skeleton 866cd81f)."""
    if os.path.isfile(CORPUS_RECEIPT_SHELL):
        return CORPUS_RECEIPT_SHELL
    return SHELL


def _pick_arial(fm: dict) -> Optional[str]:
    for k in fm:
        if "Arial" in k:
            return k
    return next(iter(fm), None) if fm else None


def _best_compress(stream: bytes) -> bytes:
    from openpdf_deflate import compress_like_jasper
    return compress_like_jasper(stream)


def build_unlocked_template(
    orig_path: Optional[str] = None,
    out_path: str = UNLOCKED,
    *,
    force_library: bool = False,
) -> str:
    from sber_dynamic import _allocate_uni_gid, _patch_sber_font

    sgl.build_library(force=force_library)
    orig_path = orig_path or _offline_shell_path()

    with open(orig_path, "rb") as fp:
        orig = fp.read()

    doc = fitz.open(orig_path)
    fm = tut._find_font_objects(doc)
    key = _pick_arial(fm)
    if not key:
        doc.close()
        raise RuntimeError("Arial font not found in shell")

    meta = fm[key]
    ff2_orig = doc.xref_stream(meta["fontfile_xref"])
    sub = tut._parse_subset_tounicode(
        doc.xref_stream(meta["tounicode_xref"]).decode("latin1", "replace"))
    uni_gid0: Dict[int, int] = {u: c for c, u in sub.items()}
    doc.close()

    lib = sgl._load()
    all_cps = {cp for cp in lib["store"] if cp > 0}
    need = set(all_cps) | set(uni_gid0.keys())
    font0 = TTFont(BytesIO(ff2_orig))
    planned = _allocate_uni_gid(uni_gid0, need, font0["maxp"].numGlyphs)
    still = [chr(cp) for cp in need if cp not in planned]
    if still:
        raise RuntimeError(f"Unlocked build: allocation failed for {''.join(still)}")

    active_gids = set(planned.values()) | {0, 3}
    for cp in planned:
        active_gids.update(sgl.get_bundle_component_gids(cp))

    ff2, uni_gid, font = _patch_sber_font(
        ff2_orig, planned, active_gids, blank_unused=False,
        planned_uni_gid=planned,
    )[:3]
    cmap = tut._build_tounicode_cmap(uni_gid)
    w_arr = tut._build_widths_array(font, sorted(active_gids))

    offsets, first, count, xref_off = tut._parse_xref_table(orig)
    sorted_xrefs = sorted(offsets.items(), key=lambda x: x[1])
    obj_ranges = {xn: (s, tut._find_object_end(orig, s)) for xn, s in sorted_xrefs}

    replacements: Dict[int, bytes] = {}
    replacements[meta["fontfile_xref"]] = tut._make_modified_obj(
        orig[obj_ranges[meta["fontfile_xref"]][0]:obj_ranges[meta["fontfile_xref"]][1]],
        meta["fontfile_xref"],
        new_stream=_best_compress(ff2), new_length1=len(ff2),
    )
    replacements[meta["tounicode_xref"]] = tut._make_modified_obj(
        orig[obj_ranges[meta["tounicode_xref"]][0]:obj_ranges[meta["tounicode_xref"]][1]],
        meta["tounicode_xref"],
        new_stream=_best_compress(cmap),
    )
    replacements[meta["cidfont_xref"]] = tut._make_modified_obj(
        orig[obj_ranges[meta["cidfont_xref"]][0]:obj_ranges[meta["cidfont_xref"]][1]],
        meta["cidfont_xref"],
        new_W=w_arr,
    )

    out = bytearray(orig[:sorted_xrefs[0][1]])
    new_offsets: Dict[int, int] = {}
    for xn, (s, e) in sorted(obj_ranges.items(), key=lambda kv: kv[1][0]):
        new_offsets[xn] = len(out)
        out.extend(replacements.get(xn, orig[s:e]))

    new_xref_off = len(out)
    xref_lines = [b"xref\n", f"{first} {count}\n".encode()]
    for n in range(first, first + count):
        if n == 0:
            xref_lines.append(b"0000000000 65535 f \n")
        elif n in new_offsets:
            xref_lines.append(f"{new_offsets[n]:010d} 00000 n \n".encode())
        else:
            xref_lines.append(b"0000000000 00000 f \n")
    out.extend(b"".join(xref_lines))

    trailer_pos = orig.find(b"trailer", xref_off)
    sx_pos = orig.find(b"startxref", trailer_pos)
    out.extend(orig[trailer_pos:sx_pos])
    out.extend(f"startxref\n{new_xref_off}\n%%EOF\n".encode())

    with open(out_path, "wb") as fp:
        fp.write(bytes(out))

    logger.info(
        "Sber unlocked: %d B (orig %d), CIDs=%d, ff2 %d -> %d B",
        len(out), len(orig), len(uni_gid), len(ff2_orig), len(ff2),
    )
    return out_path


def build_runtime_shell(
    orig_path: Optional[str] = None,
    out_path: str = RUNTIME,
    *,
    force_library: bool = False,
) -> str:
    """Offline full-charset runtime shell (content-only generation at runtime)."""
    import shutil

    unlocked = build_unlocked_template(
        orig_path=orig_path,
        out_path=UNLOCKED,
        force_library=force_library,
    )
    shutil.copy2(unlocked, out_path)
    logger.info("Sber runtime shell: %s (%d B)", out_path, os.path.getsize(out_path))
    return out_path
