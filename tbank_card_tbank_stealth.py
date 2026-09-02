"""
T-BANK «Клиенту Т-Банка» (перевод по карте внутри Т-Банка).

Пайплайн как card_sber / SBP:
  donor-orig (банк PDF) → dynamic (jasper shell) → orig-pool.
"""

import os
import re
import logging
from typing import Dict, List, Optional, Tuple

from tbank_stealth_v3 import (
    _best_compress,
    _fmt_coord,
    _fmt_date,
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
CARD_TBANK_ORIG = os.path.join(_DIR, "templates", "T_card_tbank_original.pdf")
_CARD_TBANK_SEED = os.path.join(
    os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "т банк", "по карте в т-банк.pdf"
)

_ORIG_DATE_CT = "17.06.2026  15:15:09"
_ORIG_AMOUNT_CT = "11 550 "
_ORIG_SENDER_CT = "Илья Петров"
_ORIG_RECEIVER_CT = "Андрей М."
_ORIG_CARD_CT = "*7015"
_ORIG_RECEIPT_CT = "Квитанция  \u2116 1-121-323-121-555"

_ABBREV_FIO_RE = re.compile(r"^([^\s]+)\s+([А-ЯЁA-Z])\.?$")


def _ensure_card_tbank_template() -> None:
    import shutil
    if os.path.isfile(CARD_TBANK_ORIG):
        return
    if os.path.isfile(_CARD_TBANK_SEED):
        os.makedirs(os.path.dirname(CARD_TBANK_ORIG), exist_ok=True)
        shutil.copy2(_CARD_TBANK_SEED, CARD_TBANK_ORIG)
        logger.info("card_tbank shell: %s", CARD_TBANK_ORIG)


def _ct_base_path() -> str:
    _ensure_card_tbank_template()
    return CARD_TBANK_ORIG


def _format_card_tail(raw: str) -> str:
    raw = raw.strip()
    if re.match(r"^\*\d{4}$", raw):
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 4:
        return f"*{digits[-4:]}"
    return raw


def _normalize_receiver(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()) or _ORIG_RECEIVER_CT


def _strip_yo_tverd(text: str) -> str:
    return text


def _prepare_ct_data(data: Dict) -> Dict:
    from tbank_sbp_stealth import _normalize_tbank_sender

    new_date = _fmt_date(str(data.get("date_time", _ORIG_DATE_CT)))

    amount_digits = re.sub(r"[^\d]", "", str(data.get("amount", "11550")))
    from tbank_dynamic import format_amount_like_template
    new_amount = format_amount_like_template(amount_digits, _ORIG_AMOUNT_CT)
    # card_tbank has no commission — Итого and Сумма must be identical.
    new_amount_total = new_amount

    receipt_raw = str(data.get("receipt_num", "авто")).strip()
    if receipt_raw.lower() in ("авто", "auto", "-", ""):
        receipt_raw = _gen_receipt_num_safe(kind="card_tbank", op_date=new_date)
        logger.info("auto receipt: %s", receipt_raw)

    sender_raw = str(data.get("sender", _ORIG_SENDER_CT))
    sender = _normalize_tbank_sender(sender_raw) or sender_raw

    sender = _strip_yo_tverd(sender)
    receiver = _normalize_receiver(str(data.get("receiver", _ORIG_RECEIVER_CT)))
    return {
        "new_date":         new_date,
        "new_amount":       new_amount,
        "new_amount_total": new_amount_total,
        "sender":           sender,
        "receiver":         receiver,
        "card":             _format_card_tail(str(data.get("card", _ORIG_CARD_CT))),
        "receipt_raw":      receipt_raw,
        "_user_sender": sender,
        "_user_receiver": receiver,
        "_shell_sender": _ORIG_SENDER_CT,
        "_shell_receiver": _ORIG_RECEIVER_CT,
    }


def _ct_reg_texts(prepared: Dict) -> list:
    p = prepared
    return [
        p["new_date"], p["sender"], p["receiver"], p["card"], p["new_amount"],
        f"Квитанция  \u2116 {p['receipt_raw']}",
    ]


def _extract_ct_fields(path: str) -> Optional[Dict[str, str]]:
    import fitz
    try:
        doc = fitz.open(path)
        lines = [l.strip() for l in doc[0].get_text().split("\n") if l.strip()]
        doc.close()
    except Exception:
        return None
    if "Клиенту Т-Банка" not in lines:
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
        "receiver": after("Получатель"),
        "card": after("Карта получателя"),
        "receipt": receipt,
    }


def _default_ct_orig() -> Dict[str, str]:
    orig = _extract_ct_fields(_ct_base_path())
    if orig:
        return orig
    return {
        "date": _ORIG_DATE_CT,
        "amount": _ORIG_AMOUNT_CT,
        "amount_total": _ORIG_AMOUNT_CT,
        "sender": _ORIG_SENDER_CT,
        "receiver": _ORIG_RECEIVER_CT,
        "card": _ORIG_CARD_CT,
        "receipt": _ORIG_RECEIPT_CT,
    }


def _find_best_ct_donor(prepared: Dict) -> Optional[Tuple[str, Dict, List[str], List[str]]]:
    from tbank_corpus import pick_donor, template_paths
    from tbank_donor_fit import adapt_card_prepared, pick_tbank_donor, amount_slot_fits

    reg_texts = _ct_reg_texts(prepared)
    paths = list(template_paths("card_tbank", _ct_base_path()) or [])
    preferred = pick_donor("card_tbank", prepared.get("new_date"))
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
        extract_orig=_extract_ct_fields,
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
    reg_texts = _ct_reg_texts(p)
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
    o_amt = orig.get("amount") or _ORIG_AMOUNT_CT
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

    for name, key in [("card", "card"), ("sender", "sender"), ("receiver", "receiver")]:
        old, new = orig[key], p[key]
        if new == old:
            ok[name] = True
        else:
            stream, ok[name] = _replace_tj_right(
                ctx, stream, old, new, 9.0, medium=False,
                preserve_layout=False)

    o_rcp = orig.get("receipt") or _ORIG_RECEIPT_CT
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
        from tbank_sbp_stealth import _patch_length_and_rebuild

        new_compressed = compress_orig_stream(stream, orig_comp_len)
        if new_compressed is None:
            return None
        if len(new_compressed) != orig_comp_len:
            rebuilt = _patch_length_and_rebuild(pdf, cs, ce, new_compressed)
            if rebuilt is None:
                return None
            pdf = bytearray(rebuilt)
        else:
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
                "🟡 CARD_TBANK %s size %d≠%d — reject",
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
    logger.info("🟡 CARD_TBANK %s [%s]: %d bytes", tag, os.path.basename(template_path), len(result))
    return result


def _try_donor_orig_ct(prepared: Dict) -> Optional[bytes]:
    hit = _find_best_ct_donor(prepared)
    if not hit:
        return None
    donor, prepared, miss_r, miss_m = hit
    if miss_r or miss_m:
        return None
    orig = _extract_ct_fields(donor)
    if not orig:
        return None
    logger.info("🔵 card_tbank donor-orig: %s", os.path.basename(donor))
    from tbank_channel_common import prepare_donor_for_orig
    from tbank_donor_fit import adapt_card_prepared

    reg_texts = _ct_reg_texts(prepared)
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
        paths = template_paths("card_tbank", _ct_base_path())
        preferred = pick_donor("card_tbank", prepared.get("new_date"))
        if preferred and preferred in paths:
            paths = [preferred] + [x for x in paths if x != preferred]
    except Exception:
        paths = [_ct_base_path()]

    default = _default_ct_orig()
    for path in paths:
        if path == _ct_base_path() and not os.path.isfile(path):
            continue
        orig = _extract_ct_fields(path) if path != _ct_base_path() else default
        if orig is None:
            orig = default if path == _ct_base_path() else None
        if orig is None:
            continue
        res = _try_orig_mode_on(path, orig, prepared, tag="ORIG")
        if res is not None:
            return res
    return None


def _build_dynamic_ct(prepared: Dict) -> Optional[bytes]:
    from tbank_dynamic import build_dynamic_tbank
    from tbank_channel_common import size_matches_original
    from tbank_sbp_stealth import (
        _F1_CARD_TBANK_DEC_LO,
        _F1_CARD_TBANK_DEC_HI,
        _F1_CARD_TBANK_DEC_TARGET,
    )

    p = prepared
    base = _ct_base_path()
    orig_f = _extract_ct_fields(base) or _default_ct_orig()
    o_receipt = orig_f.get("receipt") or _ORIG_RECEIPT_CT

    need_r = _ct_reg_texts(p)

    def apply(stream, er, em, rtj, replace_once, er_soft=None):
        ok: Dict[str, bool] = {}
        enc_r = er_soft or er
        stream, ok["date"] = replace_once(stream, _ORIG_DATE_CT, p["new_date"])
        stream, ok["amt_big"] = rtj(stream, _ORIG_AMOUNT_CT, p["new_amount_total"], 16.0, True)
        stream, ok["amt_small"] = rtj(stream, _ORIG_AMOUNT_CT, p["new_amount"], 9.0, False)
        for name, old, new in [
            ("card", _ORIG_CARD_CT, p["card"]),
            ("sender", _ORIG_SENDER_CT, p["sender"]),
            ("receiver", _ORIG_RECEIVER_CT, p["receiver"]),
        ]:
            stream, ok[name] = rtj(stream, old, new, 9.0, False)
        stream, ok["receipt"] = _replace_card_receipt(
            stream, enc_r, o_receipt, p["receipt_raw"], enc_r_old=er,
        )
        return stream, ok

    return build_dynamic_tbank(
        orig_path=base,
        need_r_texts=need_r,
        need_m_texts=[p["new_amount_total"], p["new_amount"]],
        apply_replacements=apply,
        metadata_date=p["new_date"],
        patch_metadata_fn=_patch_pdf_metadata,
        log_label="🟡 CARD_TBANK DYNAMIC",
        f1_dec_band=(_F1_CARD_TBANK_DEC_LO, _F1_CARD_TBANK_DEC_HI),
        f1_dec_target=_F1_CARD_TBANK_DEC_TARGET,
        f1_blank_on_trim=True,
        # Proton TBANK_CLIENT_CONTENT_SIZE_OUTLIER: h=431 Клиенту atlas 3129–3155.
        cs_dec_min=3129,
        cs_dec_max=3155,
        post_process=lambda pdf: (
            pdf
            if size_matches_original(pdf, base, drift=2048)
            else (logger.warning("CARD_TBANK size drift %d — ship", len(pdf)) or pdf)
        ),
    )


def create_tbank_card_tbank_stealth(data: Dict) -> Optional[bytes]:
    """PDF «Клиенту Т-Банка» — donor-orig → dynamic (как SBP)."""
    from tbank_dynamic import create_tbank_pipeline
    from tbank_channel_common import pdf_face_matches_user

    prepared = _prepare_ct_data(data)
    pdf = create_tbank_pipeline(
        prepared,
        [_try_donor_orig_ct, _build_dynamic_ct],
        channel="card_tbank",
        dynamic_builder=_build_dynamic_ct,
    )
    if pdf is None:
        try:
            pdf = _build_dynamic_ct(dict(prepared))
        except Exception as exc:
            logger.error("CARD_TBANK LAW1 dynamic retry: %s", exc)
            pdf = None
    if pdf is None:
        return None
    ok, why = pdf_face_matches_user(
        pdf, prepared, donor_senders=(_ORIG_SENDER_CT,),
    )
    if not ok:
        logger.error("CARD_TBANK: %s — ship anyway", why)
    return pdf


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    sample = {
        "date_time": "18.05.2026, 02:18",
        "amount": "23500",
        "sender": "Никита Васильев",
        "receiver": "Андрей Т.",
        "card": "*6993",
        "receipt_num": "авто",
    }
    res = create_tbank_card_tbank_stealth(sample)
    if res:
        out = os.path.join(_DIR, "test_card_tbank.pdf")
        with open(out, "wb") as f:
            f.write(res)
        print(f"saved: {out} ({len(res)} bytes)")
    else:
        print("FAIL")
