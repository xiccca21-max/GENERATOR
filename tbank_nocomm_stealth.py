"""
T-BANK «по карте в другой банк (без к-и)».

Пайплайн как SBP / card_sber / card_tbank / phone:
  donor-orig (банк PDF) → dynamic (jasper shell) → orig-pool.

Поля: дата, сумма, отправитель, карта, квитанция.
НЕ ТРОГАЕМ: «На карту», «Успешно» — нет получателя/банка/комиссии.
"""

import os
import re
import logging
from typing import Dict, List, Optional, Tuple

from tbank_stealth_v3 import (
    _best_compress,
    _fmt_coord,
    _fmt_date,
    _format_card_num,
    _gen_receipt_num_safe,
    _pad_pdf_to_target,
    _pad_to_compressed_size,
    _patch_length_and_rebuild,
    _patch_pdf_metadata,
    _replace_card_receipt,
    _replace_once,
    _TM_RE,
    _verify_card_receipt_in_pdf,
)

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
NOCOMM_ORIG = os.path.join(_DIR, "templates", "T_nocomm_original.pdf")
_NOCOMM_SEED = os.path.join(
    os.path.expanduser("~"),
    "OneDrive", "Desktop", "чеки", "т банк",
    "по карте в другой банк (без к-и) 1.pdf",
)

_ORIG_DATE_NC = "16.04.2026  00:03:53"
_ORIG_AMOUNT_NC = "6 100 "
_ORIG_SENDER_NC = "Боровицкий Александр"
_ORIG_CARD_NC = "220003******7046"
_ORIG_RECEIPT_NC = "Квитанция  \u2116 1-113-249-044-779"

# Corpus F1.FontFile2.decoded for nocomm originals (not SBP V3 17120–17198).
_F1_NOCOMM_DEC_LO = 14863  # exclusive floor for peel (Proton HARD ≥14864)
# Proton HARD ceiling 15840 (atlas 14864–15840); never soft-ship above.
_F1_NOCOMM_DEC_HI = 15840
_F1_NOCOMM_DEC_TARGET = 15188
_F2_NOCOMM_DEC_LO = 5056
_F2_NOCOMM_DEC_HI = 5512
_F1_NOCOMM_HARD_LO = 14864  # inclusive Proton HARD


def _ensure_nocomm_template() -> None:
    import shutil
    if os.path.isfile(NOCOMM_ORIG):
        try:
            import fitz
            kw = fitz.open(NOCOMM_ORIG).metadata.get("keywords", "")
            if "| 991" in kw or "DOCS-2035" in kw:
                return
        except Exception:
            pass
    if os.path.isfile(_NOCOMM_SEED):
        os.makedirs(os.path.dirname(NOCOMM_ORIG), exist_ok=True)
        shutil.copy2(_NOCOMM_SEED, NOCOMM_ORIG)
        logger.info("nocomm shell: %s", NOCOMM_ORIG)


def _nocomm_base_path() -> str:
    _ensure_nocomm_template()
    return NOCOMM_ORIG


def _strip_yo_tverd(text: str) -> str:
    return text


def _prepare_nc_data(data: Dict) -> Dict:
    from tbank_sbp_stealth import _normalize_tbank_sender

    new_date = _fmt_date(str(data.get("date_time", _ORIG_DATE_NC)))

    fmt_ref = (_extract_nc_fields(_nocomm_base_path()) or {}).get("amount") or _ORIG_AMOUNT_NC
    amount_digits = re.sub(r"[^\d]", "", str(data.get("amount", "6100")))
    from tbank_dynamic import format_amount_like_template
    new_amount = format_amount_like_template(amount_digits, fmt_ref)

    total_raw = str(data.get("amount_total", "")).strip()
    if total_raw:
        total_digits = re.sub(r"[^\d]", "", total_raw)
        new_amount_total = format_amount_like_template(
            total_digits or amount_digits, fmt_ref)
    else:
        new_amount_total = new_amount

    receipt_raw = str(data.get("receipt_num", "авто")).strip()
    if receipt_raw.lower() in ("авто", "auto", "-", ""):
        receipt_raw = _gen_receipt_num_safe(kind="nocomm", op_date=new_date)
        logger.info("auto receipt: %s", receipt_raw)

    sender_raw = str(data.get("sender", _ORIG_SENDER_NC))
    sender = _normalize_tbank_sender(sender_raw) or sender_raw

    return {
        "new_date":         new_date,
        "new_amount":       new_amount,
        "new_amount_total": new_amount_total,
        "sender":           _strip_yo_tverd(sender),
        "card":             _format_card_num(str(data.get("card", _ORIG_CARD_NC))),
        "receipt_raw":      receipt_raw,
    }


def _nc_reg_texts(prepared: Dict) -> list:
    p = prepared
    return [
        p["new_date"], p["sender"], p["card"], p["new_amount"],
        f"Квитанция  \u2116 {p['receipt_raw']}",
    ]


def _extract_nc_fields(path: str) -> Optional[Dict[str, str]]:
    import fitz
    try:
        doc = fitz.open(path)
        lines = [l.strip() for l in doc[0].get_text().split("\n") if l.strip()]
        doc.close()
    except Exception:
        return None
    if "На карту" not in lines:
        return None

    def after(label: str) -> str:
        for i, l in enumerate(lines):
            if l == label and i + 1 < len(lines):
                return lines[i + 1]
        return ""

    amount_big = ""
    for i, l in enumerate(lines):
        if l == "Итого" and i + 1 < len(lines):
            amount_big = lines[i + 1].replace("i", "").strip() + " "
            break
    amount_small = ""
    for i, l in enumerate(lines):
        if l == "Сумма" and i > 0:
            prev = lines[i - 1].replace("i", "").strip()
            if prev and prev[0].isdigit():
                amount_small = prev + " "
            break
    receipt = next((l for l in lines if l.startswith("Квитанция")), "")
    amt = amount_small or amount_big
    return {
        "date": lines[0],
        "amount": amt,
        "amount_total": amount_big or amt,
        "sender": after("Отправитель"),
        "card": after("Карта получателя"),
        "receipt": receipt,
    }


def _default_nc_orig() -> Dict[str, str]:
    orig = _extract_nc_fields(_nocomm_base_path())
    if orig:
        return orig
    return {
        "date": _ORIG_DATE_NC,
        "amount": _ORIG_AMOUNT_NC,
        "amount_total": _ORIG_AMOUNT_NC,
        "sender": _ORIG_SENDER_NC,
        "card": _ORIG_CARD_NC,
        "receipt": _ORIG_RECEIPT_NC,
    }


def _find_best_nc_donor(prepared: Dict) -> Optional[Tuple[str, Dict, List[str], List[str]]]:
    from tbank_corpus import pick_donor, template_paths
    from tbank_donor_fit import adapt_card_prepared, pick_tbank_donor, amount_slot_fits

    reg_texts = _nc_reg_texts(prepared)
    paths = list(template_paths("nocomm", _nocomm_base_path()) or [])
    preferred = pick_donor("nocomm", prepared.get("new_date"))
    if preferred and preferred in paths:
        paths = [preferred] + [p for p in paths if p != preferred]

    def _score(ctx, orig, adapted):
        score = 40
        if orig.get("date", "").strip() == adapted["new_date"].strip():
            score += 60
        return score

    return pick_tbank_donor(
        paths,
        prepared,
        adapt_fn=adapt_card_prepared,
        reg_texts=reg_texts,
        med_texts=[prepared["new_amount_total"], prepared["new_amount"]],
        extract_orig=_extract_nc_fields,
        score_fn=_score,
        min_score=20,
        amount_slot_ok=lambda ctx, oa, na, med: amount_slot_fits(oa, na),
        amount_total_key="new_amount_total",
        allow_font_extend=True,
        max_miss_r=0,
    )


def _replace_tj_right(ctx, stream, old, new, sz, medium=False, *, preserve_layout=False, amount=False):
    if preserve_layout:
        from tbank_orig_mode import replace_amount_preserve_tm, replace_field_preserve_tm
        if amount:
            stream, _, ok = replace_amount_preserve_tm(ctx, stream, old, new, medium=medium)
            return stream, ok
        stream, fitted, ok = replace_field_preserve_tm(
            ctx, stream, old, new, medium=medium, allow_trim=False,
        )
        if ok and (fitted or "").rstrip() != (new or "").rstrip():
            return stream, False
        return stream, ok
    enc = lambda t: ctx.enc(t, medium=medium)
    enc_old, enc_new = enc(old), enc(new)
    needle = b"(" + enc_old + b")Tj"
    pos = stream.find(needle)
    if pos < 0:
        return stream, False
    if enc_old == enc_new:
        return stream, True
    look_from = max(0, pos - 200)
    region = stream[look_from:pos]
    tms = list(_TM_RE.finditer(region))
    if not tms:
        return stream[:pos] + b"(" + enc_new + b")Tj" + stream[pos + len(needle):], True
    last = tms[-1]
    old_x = float(last.group(1))
    old_y = last.group(2).decode()
    old_w = ctx.text_width(old, sz, medium=medium)
    new_w = ctx.text_width(new, sz, medium=medium)
    new_x = old_x + (old_w - new_w)
    new_tm = f"1 0 0 1 {_fmt_coord(new_x)} {old_y} Tm".encode("ascii")
    return (
        stream[:look_from + last.start()] + new_tm
        + stream[look_from + last.end():pos] + b"(" + enc_new + b")Tj"
        + stream[pos + len(needle):]
    ), True


def _try_orig_mode_on(
    template_path: str,
    orig: Dict[str, str],
    prepared: Dict,
    tag: str = "ORIG",
    preserve_donor: bool = False,
    preserve_metadata: bool = False,
    skip_size_pad: bool = False,
) -> Optional[bytes]:
    from tbank_orig_mode import OrigContext
    from tbank_sbp_stealth import _fix_stream_separators, _prune_donor_font_subset, _pad_pdf_to_exact_size

    ctx = OrigContext()
    if not ctx.load(template_path):
        return None

    p = prepared
    reg_texts = _nc_reg_texts(p)
    ok_r, miss_r = ctx.can_render_reg(*reg_texts)
    ok_m, miss_m = ctx.can_render_med(p["new_amount_total"], p["new_amount"])
    if not (ok_r and ok_m):
        logger.info(
            "  [%s %s] skip: reg=%s med=%s",
            tag, os.path.basename(template_path), miss_r, miss_m,
        )
        return None

    pdf = bytearray(ctx.pdf_bytes)
    donor_size = len(ctx.pdf_bytes)
    cs, ce, raw = ctx.cs_cs, ctx.cs_ce, ctx.cs_raw
    stream = bytes(ctx.cs_dec)
    orig_comp_len = len(raw)
    o_amt = orig.get("amount") or _ORIG_AMOUNT_NC
    o_total = orig.get("amount_total") or o_amt

    ok: Dict[str, bool] = {}
    pm = preserve_donor or preserve_metadata
    if p["new_date"] == orig.get("date"):
        ok["date"] = True
    else:
        if pm:
            from tbank_orig_mode import replace_field_preserve_tm
            stream, _, ok["date"] = replace_field_preserve_tm(
                ctx, stream, orig["date"], p["new_date"], medium=False,
            )
        else:
            stream, ok["date"] = _replace_once(
                stream, ctx.enc(orig["date"]), ctx.enc(p["new_date"]))

    if p["new_amount"].strip() == o_amt.strip():
        ok["amt_big"] = ok["amt_small"] = True
    else:
        pl = pm
        stream, ok["amt_big"] = _replace_tj_right(
            ctx, stream, o_total, p["new_amount_total"], 16.0, medium=True,
            preserve_layout=pl, amount=True)
        stream, ok["amt_small"] = _replace_tj_right(
            ctx, stream, o_amt, p["new_amount"], 9.0, medium=False,
            preserve_layout=pl, amount=True)

    for name, key in [("sender", "sender"), ("card", "card")]:
        old, new = orig[key], p[key]
        if new == old:
            ok[name] = True
        else:
            stream, ok[name] = _replace_tj_right(
                ctx, stream, old, new, 9.0, medium=False,
                preserve_layout=False)

    o_rcp = orig.get("receipt") or _ORIG_RECEIPT_NC
    stream, ok["receipt"] = _replace_card_receipt(
        stream, lambda t: ctx.enc(t), o_rcp, p["receipt_raw"])

    for k, v in ok.items():
        logger.info("  [%s] %s %s", tag, "OK" if v else "FAIL", k)
    if not all(ok.values()):
        return None

    if not pm:
        from tbank_sbp_stealth import _normalize_content_stream_footer
        stream = _normalize_content_stream_footer(stream)

    if stream == ctx.cs_dec:
        pdf[cs:ce] = raw
    else:
        from tbank_channel_common import compress_orig_stream

        new_compressed = compress_orig_stream(stream, orig_comp_len)
        if new_compressed is None or len(new_compressed) != orig_comp_len:
            return None
        pdf[cs:ce] = new_compressed

    from tbank_channel_common import finalize_orig_result

    if pm:
        result = finalize_orig_result(
            bytes(pdf),
            new_date=p["new_date"],
            preserve_donor=True,
            skip_size_pad=skip_size_pad,
            donor_size=donor_size,
        )
        if donor_size and len(result) != donor_size:
            logger.warning(
                "🟡 NOCOMM %s size %d≠%d — reject",
                tag, len(result), donor_size,
            )
            return None
    else:
        result = finalize_orig_result(
            bytes(pdf),
            new_date=p["new_date"],
            preserve_donor=False,
            skip_size_pad=True,
            donor_size=0,
            ctx=ctx,
            stream=stream,
            prune=True,
        )
    logger.info("🟡 NOCOMM %s [%s]: %d bytes", tag, os.path.basename(template_path), len(result))
    return result


def _try_donor_orig_nc(prepared: Dict) -> Optional[bytes]:
    hit = _find_best_nc_donor(prepared)
    if not hit:
        return None
    donor, prepared, miss_r, miss_m = hit
    if miss_r or miss_m:
        return None
    orig = _extract_nc_fields(donor)
    if not orig:
        return None
    logger.info("🔵 nocomm donor-orig: %s", os.path.basename(donor))
    from tbank_channel_common import prepare_donor_for_orig
    from tbank_donor_fit import adapt_card_prepared

    reg_texts = _nc_reg_texts(prepared)
    hit2 = prepare_donor_for_orig(
        donor,
        prepared,
        adapt_fn=adapt_card_prepared,
        reg_texts=reg_texts,
        med_texts=[prepared["new_amount_total"], prepared["new_amount"]],
        allow_font_extend=True,
    )
    if not hit2:
        return None
    pdf_path, prepared, skip_size_pad = hit2
    return _try_orig_mode_on(
        pdf_path, orig, prepared, tag="DONOR-ORIG",
        preserve_metadata=True, preserve_donor=True,
        skip_size_pad=skip_size_pad,
    )


def _try_orig_mode(prepared: Dict) -> Optional[bytes]:
    try:
        from tbank_corpus import template_paths, pick_donor
        paths = template_paths("nocomm", _nocomm_base_path())
        preferred = pick_donor("nocomm", prepared.get("new_date"))
        if preferred and preferred in paths:
            paths = [preferred] + [x for x in paths if x != preferred]
    except Exception:
        paths = [_nocomm_base_path()]

    default = _default_nc_orig()
    for path in paths:
        if path == _nocomm_base_path() and not os.path.isfile(path):
            continue
        orig = _extract_nc_fields(path) if path != _nocomm_base_path() else default
        if orig is None:
            orig = default if path == _nocomm_base_path() else None
        if orig is None:
            continue
        res = _try_orig_mode_on(path, orig, prepared, tag="ORIG")
        if res is not None:
            return res
    return None


def _build_dynamic_nc(prepared: Dict) -> Optional[bytes]:
    from tbank_dynamic import build_dynamic_tbank
    from tbank_channel_common import size_matches_original

    p = prepared
    base = _nocomm_base_path()
    orig_f = _extract_nc_fields(base) or _default_nc_orig()
    o_date = orig_f["date"]
    o_amt = orig_f["amount"]
    o_amt_total = orig_f.get("amount_total") or o_amt
    o_sender = orig_f["sender"]
    o_card = orig_f["card"]
    o_receipt = orig_f.get("receipt") or _ORIG_RECEIPT_NC

    need_r = _nc_reg_texts(p)

    def apply(stream, er, em, rtj, replace_once, _er_soft=None):
        ok: Dict[str, bool] = {}
        stream, ok["date"] = replace_once(stream, o_date, p["new_date"])
        stream, ok["amt_big"] = rtj(stream, o_amt_total, p["new_amount_total"], 16.0, True)
        stream, ok["amt_small"] = rtj(stream, o_amt, p["new_amount"], 9.0, False)
        stream, ok["sender"] = rtj(stream, o_sender, p["sender"], 9.0, False)
        stream, ok["card"] = rtj(stream, o_card, p["card"], 9.0, False)
        stream, ok["receipt"] = _replace_card_receipt(stream, er, o_receipt, p["receipt_raw"])
        return stream, ok

    return build_dynamic_tbank(
        orig_path=base,
        need_r_texts=need_r,
        need_m_texts=[p["new_amount_total"], p["new_amount"]],
        apply_replacements=apply,
        metadata_date=p["new_date"],
        patch_metadata_fn=_patch_pdf_metadata,
        log_label="🟡 NOCOMM DYNAMIC",
        # Nocomm corpus F1.dec ≈14864–15840 (NOT SBP V3 17120–17198).
        f1_dec_band=(_F1_NOCOMM_DEC_LO, _F1_NOCOMM_DEC_HI),
        f1_dec_target=_F1_NOCOMM_DEC_TARGET,
        f1_blank_on_trim=True,
        post_process=lambda pdf: (
            pdf
            if size_matches_original(pdf, base, drift=2048)
            else (logger.warning("NOCOMM size drift %d — ship", len(pdf)) or pdf)
        ),
    )


def create_tbank_nocomm_stealth(data: Dict) -> Optional[bytes]:
    """PDF «по карте в другой банк (без к-и)» — donor-orig → dynamic (как SBP)."""
    from tbank_dynamic import create_tbank_pipeline
    from tbank_channel_common import pdf_face_matches_user

    prepared = _prepare_nc_data(data)
    pdf = create_tbank_pipeline(
        prepared,
        [_try_donor_orig_nc, _build_dynamic_nc],
        channel="nocomm",
        dynamic_builder=_build_dynamic_nc,
    )
    if not pdf:
        return None
    lean = _lean_nocomm_fonts_after_face(pdf, prepared)
    if lean is None:
        logger.error("nocomm lean miss — ship unleaned")
        lean = pdf
    if not _nocomm_font_sizes_ok(lean):
        logger.error("nocomm F1/F2 size gate — ship anyway")
    ok, why = pdf_face_matches_user(
        lean, prepared, donor_senders=(_ORIG_SENDER_NC,),
    )
    if not ok:
        logger.error("NOCOMM: %s — ship anyway", why)
    return lean


def _nocomm_font_sizes_ok(pdf: bytes) -> bool:
    import fitz
    import tbank_unlock_template as tut

    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        fm = tut._find_font_objects(doc)
        ok = True
        meta = fm.get("TinkoffSans-Regular")
        if meta:
            n = len(doc.xref_stream(meta["fontfile_xref"]))
            if not (_F1_NOCOMM_HARD_LO <= n <= _F1_NOCOMM_DEC_HI):
                logger.warning("nocomm F1 size gate %d", n)
                ok = False
        meta = fm.get("TinkoffSans-Medium")
        if meta:
            n = len(doc.xref_stream(meta["fontfile_xref"]))
            if n > _F2_NOCOMM_DEC_HI or n < _F2_NOCOMM_DEC_LO:
                logger.warning("nocomm F2 size gate %d", n)
                ok = False
        doc.close()
        return ok
    except Exception:
        return False


def _lean_nocomm_fonts_after_face(pdf: bytes, prepared: Dict) -> Optional[bytes]:
    """Peel F1/F2 into nocomm HARD decoded bands after donor-orig inject growth."""
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _TBANK_F1_OPENPDF_CSA,
        _blank_ff2_unused_glyfs,
        _ff2_composite_closure,
        _ff2_restore_shell_tables,
        _head_bbox_from_ff2,
        _patch_fontfile2_xref,
        _restore_head_bbox,
        _restore_head_csa,
        _trim_f1_into_v3_window,
        _ttf_num_glyphs,
    )

    parts = list(_nc_reg_texts(prepared))
    for k in ("new_amount", "new_amount_total", "sender", "card"):
        v = prepared.get(k) or ""
        if v:
            parts.append(str(v))
    parts.append("Перевод На карту Статус Успешно Сумма Отправитель Итого Квитанция")
    keep_cps = {ord(ch) for ch in "".join(parts)}
    digits = {ord(ch) for ch in "".join(
        str(prepared.get(k) or "") for k in ("new_amount", "new_amount_total")
    ) if ch.isdigit()}
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        fm = tut._find_font_objects(doc)
        out = pdf
        # --- F1 ---
        meta = fm.get("TinkoffSans-Regular")
        if meta:
            ff_xref = meta["fontfile_xref"]
            tu_xref = meta["tounicode_xref"]
            ff2 = doc.xref_stream(ff_xref)
            if len(ff2) > _F1_NOCOMM_DEC_HI:
                sub = tut._parse_subset_tounicode(
                    doc.xref_stream(tu_xref).decode("latin1", "replace")
                )
                uni = {u: c for c, u in sub.items()}
                keep = {0, 3}
                for cp in keep_cps:
                    cid = uni.get(cp)
                    if cid is not None:
                        keep.add(int(cid))
                try:
                    keep = _ff2_composite_closure(ff2, keep)
                except Exception:
                    pass
                lean = _trim_f1_into_v3_window(
                    ff2, keep, lo=_F1_NOCOMM_DEC_LO, hi=_F1_NOCOMM_DEC_HI,
                )
                lean = _ff2_restore_shell_tables(ff2, lean)
                bb = _head_bbox_from_ff2(ff2)
                if bb is not None:
                    lean = _restore_head_bbox(lean, bb)
                if _ttf_num_glyphs(lean) > 200:
                    lean = _restore_head_csa(lean, _TBANK_F1_OPENPDF_CSA)
                if not (_F1_NOCOMM_HARD_LO <= len(lean) <= _F1_NOCOMM_DEC_HI):
                    logger.warning("nocomm F1 lean miss %d→%d", len(ff2), len(lean))
                    doc.close()
                    return None
                patched = _patch_fontfile2_xref(out, ff_xref, lean)
                if patched is None:
                    doc.close()
                    return None
                logger.info("nocomm F1 lean %d→%d", len(ff2), len(lean))
                out = patched
                doc.close()
                doc = fitz.open(stream=out, filetype="pdf")
                fm = tut._find_font_objects(doc)
        # --- F2 ---
        meta = fm.get("TinkoffSans-Medium")
        if meta:
            ff_xref = meta["fontfile_xref"]
            tu_xref = meta["tounicode_xref"]
            ff2 = doc.xref_stream(ff_xref)
            if len(ff2) > _F2_NOCOMM_DEC_HI:
                sub = tut._parse_subset_tounicode(
                    doc.xref_stream(tu_xref).decode("latin1", "replace")
                )
                uni = {u: c for c, u in sub.items()}
                keep = {0, 3}
                for cp in ({ord(ch) for ch in "Итого"} | {0x20} | digits):
                    cid = uni.get(cp)
                    if cid is not None:
                        keep.add(int(cid))
                try:
                    keep = _ff2_composite_closure(ff2, keep)
                except Exception:
                    pass
                lean = _blank_ff2_unused_glyfs(ff2, keep)
                lean = _ff2_restore_shell_tables(ff2, lean)
                bb = _head_bbox_from_ff2(ff2)
                if bb is not None:
                    lean = _restore_head_bbox(lean, bb)
                if not (_F2_NOCOMM_DEC_LO <= len(lean) <= _F2_NOCOMM_DEC_HI):
                    logger.warning(
                        "nocomm F2 lean miss %d→%d (want %d..%d)",
                        len(ff2), len(lean), _F2_NOCOMM_DEC_LO, _F2_NOCOMM_DEC_HI,
                    )
                    doc.close()
                    return None
                patched = _patch_fontfile2_xref(out, ff_xref, lean)
                if patched is None:
                    doc.close()
                    return None
                logger.info("nocomm F2 lean %d→%d", len(ff2), len(lean))
                out = patched
        doc.close()
        return out
    except Exception as exc:
        logger.warning("nocomm font lean failed: %s", exc)
        return None


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    sample = {
        "date_time": "18.05.2026, 02:18",
        "amount": "12500",
        "sender": "Иван Иванов И.",
        "card": "220220******1234",
        "receipt_num": "авто",
    }
    res = create_tbank_nocomm_stealth(sample)
    if res:
        out = os.path.join(_DIR, "test_nocomm.pdf")
        with open(out, "wb") as f:
            f.write(res)
        print(f"saved: {out} ({len(res)} bytes)")
    else:
        print("FAIL")
