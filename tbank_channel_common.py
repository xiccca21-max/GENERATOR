"""Общая инфраструктура T-Bank каналов (кроме SBP) — тот же пайплайн, что у SBP."""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def pdf_face_matches_user(
    pdf: bytes,
    prepared: Dict,
    *,
    donor_senders: Tuple[str, ...] = (),
    donor_banks: Tuple[str, ...] = (),
    sender_key: str = "sender",
    bank_key: str = "bank",
    receiver_key: str = "receiver",
) -> Tuple[bool, str]:
    """Reject donor-face leak / truncated FIO across T-Bank card/phone/nocomm."""
    import re

    try:
        import fitz
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text() or ""
        doc.close()
    except Exception as exc:
        return False, f"face-read:{exc}"
    flat = re.sub(r"\s+", " ", text.replace("\u202f", " ")).strip()
    sender = str(prepared.get(sender_key) or "").strip()
    bank = str(prepared.get(bank_key) or "").strip()
    receiver = str(prepared.get(receiver_key) or "").strip()
    try:
        from tbank_sbp_stealth import _TBANK_TWIN_LOOKALIKE
        _map = _TBANK_TWIN_LOOKALIKE
    except Exception:
        _map = str.maketrans("")

    def _visible(want: str) -> bool:
        if not want:
            return True
        if want in flat:
            return True
        rem = want.translate(_map) if _map else want
        if rem and rem in flat:
            return True
        toks = [t for t in (rem or want).split() if len(t) >= 2]
        return bool(toks) and all(t in flat for t in toks)

    for ds in donor_senders:
        if ds and sender and sender != ds and ds in text:
            return False, f"donor-sender-leak:{ds!r}"
    for db in donor_banks:
        if db and bank and bank != db and db in text:
            return False, f"donor-bank-leak:{db!r}"
    if sender and not _visible(sender):
        return False, f"sender-missing:{sender!r}"
    if receiver and not _visible(receiver):
        return False, f"receiver-missing:{receiver!r}"
    if bank and not _visible(bank):
        return False, f"bank-missing:{bank!r}"
    return True, "ok"


def size_matches_original(pdf: bytes, original_path: str, *, drift: int = 2048) -> bool:
    """Keep shipped bytes in the channel donor's original-size band."""
    import os

    try:
        original_size = os.path.getsize(original_path)
    except OSError:
        return False
    ok = abs(len(pdf) - original_size) <= drift
    if not ok:
        logger.warning(
            "T-Bank size outlier: got=%d original=%d drift=%d",
            len(pdf), original_size, drift,
        )
    return ok


def payload_missing_contours(
    pdf_path: str,
    ctx,
    *,
    reg_texts: List[str],
    med_texts: List[str],
) -> Tuple[List[str], List[str]]:
    """Return exact payload chars whose mapped CIDs have empty contours."""
    import fitz
    from tbank_sbp_stealth import _missing_glyph_contours

    doc = fitz.open(pdf_path)
    try:
        ff_r = doc.xref_stream(ctx.fontfile_xref_reg)
        ff_m = doc.xref_stream(ctx.fontfile_xref_med)
    finally:
        doc.close()
    need_r = {ord(ch) for text in reg_texts for ch in text}
    need_m = {ord(ch) for text in med_texts for ch in text}
    return (
        _missing_glyph_contours(
            ff_r, need_r, is_medium=False, uni_gid_plan=ctx.uni_to_cid_reg,
        ),
        _missing_glyph_contours(
            ff_m, need_m, is_medium=True, uni_gid_plan=ctx.uni_to_cid_med,
        ),
    )


def patch_channel_metadata(pdf: bytes, date_time: str) -> bytes:
    """Metadata как у SBP: CreationDate + Keywords (unique hash, size-neutral)."""
    from tbank_sbp_stealth import _patch_pdf_metadata

    return _patch_pdf_metadata(pdf, date_time)


def patch_keywords_hash_only(pdf: bytes) -> bytes:
    """Unique Keywords hash in-place; keep CreationDate/ModDate byte-identical."""
    import hashlib
    import os
    import re

    m_kw = re.search(rb"/Keywords\s*\(([^)]*)\)", pdf)
    if not m_kw:
        return pdf
    old_inner = m_kw.group(1)
    # Expect «date | hash | tail»
    try:
        text = old_inner.decode("latin1")
    except Exception:
        return pdf
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 3:
        return pdf
    new_hash = hashlib.md5(os.urandom(16)).hexdigest()
    new_text = f"{parts[0]} | {new_hash} | {parts[2]}"
    new_inner = new_text.encode("latin1")
    if len(new_inner) != len(old_inner):
        # Fit exactly: pad/truncate hash field.
        budget = len(old_inner)
        base = f"{parts[0]} |  | {parts[2]}".encode("latin1")
        keep = budget - len(base)
        if keep < 8:
            return pdf
        new_inner = f"{parts[0]} | {new_hash[:keep]} | {parts[2]}".encode("latin1")
        if len(new_inner) < budget:
            new_inner = new_inner + (b"0" * (budget - len(new_inner)))
        new_inner = new_inner[:budget]
    return pdf[: m_kw.start(1)] + new_inner + pdf[m_kw.end(1) :]


def prepare_donor_for_orig(
    donor: str,
    prepared: Dict,
    *,
    adapt_fn: Callable,
    reg_texts: List[str],
    med_texts: List[str],
    allow_font_extend: bool = True,
) -> Optional[Tuple[str, Dict, bool]]:
    """Donor-orig prep как у SBP: F1/F2 swap из корпуса при нехватке глифов."""
    from tbank_orig_mode import OrigContext
    from tbank_sbp_stealth import _extend_donor_font_chars

    ctx = OrigContext()
    if not ctx.load(donor):
        return None

    ok_r, miss_r = ctx.can_render_reg(*reg_texts)
    ok_m, miss_m = ctx.can_render_med(*med_texts)
    contour_r, contour_m = payload_missing_contours(
        donor, ctx, reg_texts=reg_texts, med_texts=med_texts,
    )
    miss_r = list(dict.fromkeys([*miss_r, *contour_r]))
    miss_m = list(dict.fromkeys([*miss_m, *contour_m]))
    ok_r, ok_m = not miss_r, not miss_m
    if not (ok_r and ok_m):
        if not allow_font_extend:
            return None
        extended = _extend_donor_font_chars(
            donor,
            ctx,
            reg_chars="".join(miss_r),
            med_chars="".join(miss_m),
        )
        if extended is None:
            return None
        pdf_path = extended
        skip_size_pad = extended != donor
        ctx = OrigContext()
        if not ctx.load(pdf_path):
            return None
        ok_r, miss_r = ctx.can_render_reg(*reg_texts)
        ok_m, miss_m = ctx.can_render_med(*med_texts)
        contour_r, contour_m = payload_missing_contours(
            pdf_path, ctx, reg_texts=reg_texts, med_texts=med_texts,
        )
        miss_r = list(dict.fromkeys([*miss_r, *contour_r]))
        miss_m = list(dict.fromkeys([*miss_m, *contour_m]))
        if miss_r or miss_m:
            logger.warning(
                "extended donor still misses contours: reg=%s med=%s",
                "".join(miss_r[:24]), "".join(miss_m[:24]),
            )
            return None
        prepared = adapt_fn(ctx, prepared)
        return pdf_path, prepared, skip_size_pad

    return donor, prepared, False


def compress_orig_stream(stream: bytes, orig_comp_len: int) -> Optional[bytes]:
    """Exact OpenPDF flate size only. None on miss — never Length-rebuild."""
    from tbank_stealth_v3 import _pad_to_compressed_size

    hit = _pad_to_compressed_size(stream, orig_comp_len)
    if hit is not None and len(hit) == orig_comp_len:
        return hit
    logger.warning(
        "cs exact flate miss: need=%d — reject (no Length rebuild)", orig_comp_len
    )
    return None


def compress_orig_stream_nopad(stream: bytes, orig_comp_len: int) -> Optional[bytes]:
    """Exact OpenPDF/Jasper flate only — never Python zlib (Fraudex structure).

    No decoded padding: if natural OpenPDF size ≠ donor /Length → None (retry receipt).
    """
    from openpdf_deflate import openpdf_deflate
    import zlib

    raw = bytes(stream)
    for level in (6, 5, 7, 4, 8, 9, 3, 2, 1):
        hit = openpdf_deflate(raw, level)
        if hit is None:
            continue
        if len(hit) != orig_comp_len:
            continue
        if hit[:2] != b"\x78\x9c":
            continue
        try:
            if len(zlib.decompress(hit)) != len(raw):
                continue
        except Exception:
            continue
        return hit
    return None


def finalize_orig_result(
    pdf: bytes,
    *,
    new_date: str,
    preserve_donor: bool,
    skip_size_pad: bool,
    donor_size: int,
    ctx=None,
    stream: Optional[bytes] = None,
    prune: bool = False,
) -> bytes:
    """Финализация orig/donor-orig: metadata, separators, exact size."""
    from tbank_sbp_stealth import (
        _fix_stream_separators,
        _pad_pdf_to_exact_size,
        _prune_donor_font_subset,
    )

    result = pdf
    if prune and ctx is not None and stream is not None:
        result = bytes(_prune_donor_font_subset(bytearray(result), ctx, stream))
    # Metadata must follow the visible receipt date for every channel. In
    # particular, /Keywords token 3 is 991 before 10.07.2026 and DOCS-2035
    # on/after that date; preserving a cross-day donor token is a hard fake.
    result = patch_channel_metadata(result, new_date)
    before = result
    result = _fix_stream_separators(result)
    if len(result) != len(before):
        result = before
    if preserve_donor and donor_size and not skip_size_pad:
        if len(result) > donor_size:
            return result
        result = _pad_pdf_to_exact_size(result, donor_size)
    return result


def create_channel_stealth(
    prepared: Optional[Dict],
    donor_builder: Callable[[Dict], Optional[bytes]],
    dynamic_builder: Callable[[Dict], Optional[bytes]],
    *,
    channel: str,
    post_validate: Optional[Callable[[bytes], None]] = None,
) -> Optional[bytes]:
    """Пайплайн donor-orig → dynamic."""
    from tbank_dynamic import create_tbank_pipeline

    return create_tbank_pipeline(
        prepared,
        [donor_builder, dynamic_builder],
        channel=channel,
        dynamic_builder=dynamic_builder,
        post_validate=post_validate,
    )
