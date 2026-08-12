"""Подгонка prepared-полей под subset донора (orig-mode без font-patch)."""
from __future__ import annotations

import os
import re
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

import fitz

from tbank_orig_mode import OrigContext, _find_stream_pos_for_xref, _parse_w_array, find_object_range

def fit_text_reg(text: str, uni: Dict[int, int]) -> str:
    """Compatibility hook: donor selection must never rewrite payload text."""
    return text


def amount_med_ok(text: str, uni: Dict[int, int]) -> bool:
    return all((not ch.isdigit()) or ord(ch) in uni for ch in text)


def fit_amount_med(text: str, uni: Dict[int, int]) -> str:
    if amount_med_ok(text, uni):
        return text
    return text


def fit_lat_id(text: str, uni: Dict[int, int]) -> str:
    """Compatibility hook: IDs are accepted exactly or the donor is rejected."""
    return text


def adapt_sbp_prepared(ctx: OrigContext, prepared: Dict) -> Dict:
    p = dict(prepared)
    uni_r = ctx.uni_to_cid_reg
    uni_m = ctx.uni_to_cid_med
    for key in ("sender", "phone", "receiver", "bank", "account", "message", "commission"):
        if key in p and isinstance(p[key], str):
            p[key] = fit_text_reg(p[key], uni_r)
    for key in ("new_amount", "new_amount_total"):
        if key in p and isinstance(p[key], str):
            p[key] = fit_amount_med(p[key], uni_m)
    # SBP ID/suffix — не подгонять под subset: иначе буквы заменяются (A→D…)
    # и валидатор не находит 32-символьный opid; недостающие глифы — font extend.
    return p


def amount_slot_fits(orig_amt: str, new_amt: str) -> bool:
    """Слот суммы донора: длина >= новой суммы, хвостовой пробел перед ₽."""
    oa = orig_amt or ""
    na = new_amt if new_amt.endswith(" ") else new_amt.rstrip() + " "
    return len(oa) >= len(na) and oa.endswith(" ")


def adapt_card_prepared(ctx: OrigContext, prepared: Dict) -> Dict:
    p = dict(prepared)
    uni_r = ctx.uni_to_cid_reg
    uni_m = ctx.uni_to_cid_med
    for key in ("sender", "receiver", "bank", "card", "new_date"):
        if key in p and isinstance(p[key], str):
            p[key] = fit_text_reg(p[key], uni_r)
    for key in ("new_amount", "new_amount_total", "new_commission"):
        if key in p and isinstance(p[key], str):
            p[key] = fit_amount_med(p[key], uni_m)
    # receipt: только цифры/дефис — не трогаем (как SBP ID)
    return p


def adapt_phone_prepared(ctx: OrigContext, prepared: Dict) -> Dict:
    p = dict(prepared)
    uni_r = ctx.uni_to_cid_reg
    uni_m = ctx.uni_to_cid_med
    for key in ("sender", "phone", "receiver", "new_date"):
        if key in p and isinstance(p[key], str):
            p[key] = fit_text_reg(p[key], uni_r)
    for key in ("new_amount", "new_amount_total"):
        if key in p and isinstance(p[key], str):
            p[key] = fit_amount_med(p[key], uni_m)
    return p


def donor_missing_chars(
    ctx: OrigContext,
    *,
    reg_texts: List[str],
    med_texts: List[str],
) -> Tuple[List[str], List[str]]:
    _, miss_r = ctx.can_render_reg(*reg_texts)
    _, miss_m = ctx.can_render_med(*med_texts)
    return miss_r, miss_m


def donor_renders(
    ctx: OrigContext,
    prepared: Dict,
    *,
    reg_texts: List[str],
    med_texts: List[str],
) -> Tuple[bool, List[str], List[str]]:
    miss_r, miss_m = donor_missing_chars(ctx, reg_texts=reg_texts, med_texts=med_texts)
    return (not miss_r and not miss_m), miss_r, miss_m


def pick_tbank_donor(
    paths: List[str],
    prepared: Dict,
    *,
    adapt_fn,
    reg_texts: List[str],
    med_texts: List[str],
    extract_orig,
    amount_slot_ok=None,
    score_fn=None,
    min_score: int = 20,
    amount_total_key: Optional[str] = None,
    fields_fit_fn=None,
    miss_r_penalty: int = 100,
    max_miss_r: int = 12,
    allow_font_extend: bool = False,
) -> Optional[Tuple[str, Dict, List[str], List[str]]]:
    """Выбор донора: F1 без патча (или с font-extend), F2 — до 4 недостающих цифр суммы.

    allow_font_extend=True: miss_r почти не штрафует — prepare_donor_for_orig дошьёт глифы.
    """
    from tbank_orig_mode import OrigContext

    best: Optional[str] = None
    best_miss_r: List[str] = []
    best_miss_m: List[str] = []
    best_score = -1
    best_adapted: Optional[Dict] = None
    r_pen = 2 if allow_font_extend else miss_r_penalty
    miss_r_cap = 40 if allow_font_extend else max_miss_r
    for path in paths:
        ctx = OrigContext()
        if not ctx.load(path):
            continue
        adapted = adapt_fn(ctx, prepared)
        miss_r, miss_m = donor_missing_chars(ctx, reg_texts=reg_texts, med_texts=med_texts)
        if len(miss_r) > miss_r_cap:
            continue
        if len(miss_m) > 4:
            continue
        orig = extract_orig(path) or {}
        if amount_slot_ok is not None:
            oa = orig.get("amount", "")
            na = adapted.get("new_amount", "")
            if not amount_slot_ok(ctx, oa, na, False) or not amount_slot_ok(ctx, oa, na, True):
                continue
            if amount_total_key:
                oat = orig.get("amount_total", oa)
                nat = adapted.get(amount_total_key, na)
                if not amount_slot_ok(ctx, oat, nat, True):
                    continue
        if fields_fit_fn and not fields_fit_fn(ctx, orig, adapted):
            continue
        score = score_fn(ctx, orig, adapted) if score_fn else 100
        score -= 3 * len(miss_m)
        score -= r_pen * len(miss_r)
        if score > best_score:
            best_score = score
            best = path
            best_miss_r = miss_r
            best_miss_m = miss_m
            best_adapted = adapted
    if best is None or best_score < min_score:
        return None
    return best, (best_adapted or prepared), best_miss_r, best_miss_m


def find_reg_font_donor(paths: List[str], reg_texts: List[str]) -> Optional[str]:
    """Корпусный PDF, чей F1 Regular уже содержит все символы reg_texts."""
    need = {ord(c) for t in reg_texts for c in t}
    best: Optional[str] = None
    best_n = 10**9
    for path in paths:
        ctx = OrigContext()
        if not ctx.load(path):
            continue
        if not need <= set(ctx.uni_to_cid_reg):
            continue
        n = len(ctx.uni_to_cid_reg)
        if n < best_n:
            best_n = n
            best = path
    return best


def swap_donor_font_reg(base_path: str, font_donor_path: str) -> Optional[str]:
    """Подменить F1 (FontFile2 + ToUnicode + /W) из корпусного донора без пересборки.

    FontFile2 goes through _patch_fontfile2_xref so /Length1 matches TTF
    (raw stream copy left Length1 stale → Fraudex structure).
    """
    if base_path == font_donor_path:
        return base_path
    from tbank_stealth_v3 import _patch_length_and_rebuild
    from tbank_sbp_stealth import _patch_cidfont_w, _patch_fontfile2_xref
    import tbank_unlock_template as tut

    ctx_b = OrigContext()
    ctx_d = OrigContext()
    if not ctx_b.load(base_path) or not ctx_d.load(font_donor_path):
        return None

    with open(font_donor_path, "rb") as f:
        donor_raw = f.read()

    pdf = bytearray(ctx_b.pdf_bytes)

    # Decompressed TTF from corpus — Length1 written correctly.
    doc_d = fitz.open(font_donor_path)
    try:
        ff2 = doc_d.xref_stream(ctx_d.fontfile_xref_reg)
        w_obj = doc_d.xref_object(ctx_d.cidfont_xref_reg)
    finally:
        doc_d.close()
    if not ff2:
        return None
    patched = _patch_fontfile2_xref(bytes(pdf), ctx_b.fontfile_xref_reg, ff2)
    if patched is None:
        return None
    pdf[:] = patched

    # ToUnicode: copy compressed bytes (no Length1).
    src = _find_stream_pos_for_xref(donor_raw, ctx_d.tounicode_xref_reg)
    dst = _find_stream_pos_for_xref(bytes(pdf), ctx_b.tounicode_xref_reg)
    if src is None or dst is None:
        return None
    patched = _patch_length_and_rebuild(pdf, dst[0], dst[1], donor_raw[src[0]:src[1]])
    if patched is None:
        return None
    pdf[:] = patched

    widths, _ = _parse_w_array(w_obj)
    if not _patch_cidfont_w(pdf, ctx_b.cidfont_xref_reg, tut._build_widths_from_map(widths)):
        pass

    fd, out = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(out, "wb") as f:
        f.write(bytes(pdf))
    return out


def swap_donor_font_med(base_path: str, font_donor_path: str) -> Optional[str]:
    """Подменить F2 (FontFile2 + ToUnicode + /W) из корпусного PDF без пересборки."""
    if base_path == font_donor_path:
        return base_path
    from tbank_stealth_v3 import _patch_length_and_rebuild
    from tbank_sbp_stealth import _patch_cidfont_w, _patch_fontfile2_xref
    import tbank_unlock_template as tut

    ctx_b = OrigContext()
    ctx_d = OrigContext()
    if not ctx_b.load(base_path) or not ctx_d.load(font_donor_path):
        return None

    with open(font_donor_path, "rb") as f:
        donor_raw = f.read()

    pdf = bytearray(ctx_b.pdf_bytes)

    doc_d = fitz.open(font_donor_path)
    try:
        ff2 = doc_d.xref_stream(ctx_d.fontfile_xref_med)
        w_obj = doc_d.xref_object(ctx_d.cidfont_xref_med)
    finally:
        doc_d.close()
    if not ff2:
        return None
    patched = _patch_fontfile2_xref(bytes(pdf), ctx_b.fontfile_xref_med, ff2)
    if patched is None:
        return None
    pdf[:] = patched

    src = _find_stream_pos_for_xref(donor_raw, ctx_d.tounicode_xref_med)
    dst = _find_stream_pos_for_xref(bytes(pdf), ctx_b.tounicode_xref_med)
    if src is None or dst is None:
        return None
    patched = _patch_length_and_rebuild(pdf, dst[0], dst[1], donor_raw[src[0]:src[1]])
    if patched is None:
        return None
    pdf[:] = patched

    widths, _ = _parse_w_array(w_obj)
    if not _patch_cidfont_w(pdf, ctx_b.cidfont_xref_med, tut._build_widths_from_map(widths)):
        pass

    fd, out = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(out, "wb") as f:
        f.write(bytes(pdf))
    return out


def prepare_card_donor_pdf(
    donor_path: str,
    prepared: Dict,
    *,
    adapt_fn: Callable,
    reg_texts: List[str],
    med_texts: List[str],
    glyph_paths: List[str],
) -> Optional[Tuple[str, Dict, bool]]:
    """
    Card-канал: F1/F2 — swap из корпуса (hash в пуле), без rebuild subset.
    """
    ctx = OrigContext()
    if not ctx.load(donor_path):
        return None

    miss_r, miss_m = donor_missing_chars(ctx, reg_texts=reg_texts, med_texts=med_texts)
    pdf_path = donor_path
    skip_size_pad = False

    if miss_r:
        font_donor = find_reg_font_donor(glyph_paths, reg_texts)
        if not font_donor:
            return None
        swapped = swap_donor_font_reg(pdf_path, font_donor)
        if not swapped:
            return None
        if swapped != pdf_path:
            skip_size_pad = True
        pdf_path = swapped
        ctx = OrigContext()
        if not ctx.load(pdf_path):
            return None
        miss_r, miss_m = donor_missing_chars(ctx, reg_texts=reg_texts, med_texts=med_texts)
        if miss_r:
            return None

    if miss_m:
        from tbank_sbp_stealth import _find_corpus_font_donor
        med_text = "".join(med_texts)
        font_donor = _find_corpus_font_donor(glyph_paths, med_text, medium=True)
        if not font_donor:
            return None
        swapped = swap_donor_font_med(pdf_path, font_donor)
        if not swapped:
            return None
        if swapped != pdf_path:
            skip_size_pad = True
        pdf_path = swapped
        ctx = OrigContext()
        if not ctx.load(pdf_path):
            return None

    return pdf_path, adapt_fn(ctx, prepared), skip_size_pad
