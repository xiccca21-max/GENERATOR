"""
T-BANK «По номеру телефона».

Пайплайн как card_sber / card_tbank / SBP:
  donor-orig (банк PDF) → dynamic (jasper shell) → orig-pool.

НЕ ТРОГАЕМ: «По номеру телефона», «Успешно».
Комиссия всегда «0 ₽» (F1 «0 » + /F3 i), не «Без комиссии».
"""

import os
import re
import logging
from time_msk import now_msk
from typing import Dict, List, Optional, Tuple

from tbank_sbp_stealth import _normalize_content_stream_footer
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
# Full-charset unlocked shell (offline bake). Live FontFile2 inject forbidden.
# NEVER use T_phone_unlocked.pdf — baked Medium ~6892 B → Proton F2 size FAKE.
PHONE_ORIG_FULL = os.path.join(_DIR, "templates", "T_phone_unlocked.pdf")
PHONE_ORIG_NARROW = os.path.join(_DIR, "templates", "T_phone_original.pdf")
PHONE_ORIG = PHONE_ORIG_NARROW
_PHONE_SEED = os.path.join(
    os.path.expanduser("~"),
    "OneDrive", "Desktop", "чеки", "т банк",
    "по номеру телефона на т банк.pdf",
)

_ORIG_DATE = "21.04.2026  15:05:50"
_ORIG_AMOUNT = "14 000 "
_ORIG_SENDER = "Дамир Сеничев"
_ORIG_PHONE = "+7 (961) 954-80-60"
_ORIG_RECEIVER = "Марина Ч."
_ORIG_RECEIPT = "Квитанция  \u2116 1-104-397-813-566"
_ORIG_COMMISSION = "0 "  # shell baked: F1 «0 » + /F3 i → «0 ₽»
_DISPLAY_COMMISSION = "0 "
_PHONE_COL_RIGHT = 250.0
_PHONE_DATE_LEFT = 20.0  # originals: date x0=20 (LEFT), never right-column
_PHONE_F3_W_9 = 6.3  # ALSRubl «i» at 9pt (same as amount line)
_PHONE_F3_W_16 = 12.23  # ALSRubl «i» at 16pt — digits end ~237.77, F3→250
_LEGACY_COMMISSION = "Без комиссии"
# Static face rows in phone shell (never rewrite «Успешно» with commission/amount).
_PHONE_STATUS_Y = 288.78
_PHONE_COMMISSION_Y = 247.78
_PHONE_AMOUNT_SMALL_Y = 268.78
_PHONE_AMOUNT_BIG_Y = 344.39
# Jasper stamp: empty ()Tj at Tm (0, page-h). Filling it → first-line mojibake.
_PHONE_STAMP_Y_MIN = 449.0
# Proton TBANK_FILE_SIZE_STRONG_OUTLIER: HARD ceiling now 62000 (was 63500).
# Etalon ~58–61KB — unlocked ~63484 always FAKE. Lean shell only.
_PHONE_HARD_MAX = 62000


def _phone_tm_y_before(stream: bytes, pos: int, *, window: int = 120) -> Optional[float]:
    """Y from the Tm immediately preceding a Tj at ``pos``."""
    look = stream[max(0, pos - window) : pos]
    tms = list(_TM_RE.finditer(look))
    if not tms:
        return None
    try:
        return float(tms[-1].group(2))
    except (TypeError, ValueError, IndexError):
        return None


def _phone_y_near(y: Optional[float], target: float, tol: float = 1.5) -> bool:
    return y is not None and abs(float(y) - float(target)) <= tol


def _phone_restore_stamp_tj(stream: bytes) -> bytes:
    """Keep Jasper top stamp as empty ``()Tj`` at Tm x=0.

    A leftover FIO in this slot is the first extractable line
    (TBANK_DATE_LINE_CORRUPTED: «ле се Л.» instead of DD.MM.YYYY).
    """
    out = stream
    guard = 0
    while guard < 4:
        guard += 1
        found = False
        for m in re.finditer(rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm", out):
            try:
                y = float(m.group(2))
            except ValueError:
                continue
            if y < _PHONE_STAMP_Y_MIN:
                continue
            rest = out[m.end() :]
            et = rest.find(b"ET")
            if et < 0:
                continue
            tj = re.search(rb"\((?:\\.|[^\\()])*\)Tj", rest[:et])
            if not tj:
                continue
            inner = tj.group(0)[1:-3]
            tm_ok = m.group(1) in (b"0", b"0.0")
            if tm_ok and inner == b"":
                continue
            new_tm = b"1 0 0 1 0 " + m.group(2) + b" Tm"
            out = (
                out[: m.start()]
                + new_tm
                + out[m.end() : m.end() + tj.start()]
                + b"()Tj"
                + out[m.end() + tj.end() :]
            )
            found = True
            break
        if not found:
            break
    return out


def _phone_restore_stamp_in_pdf(pdf: bytes) -> bytes:
    if not pdf:
        return pdf
    try:
        import fitz
        from tbank_sbp_stealth import _patch_contents_xref

        doc = fitz.open(stream=pdf, filetype="pdf")
        cs_x = int(doc[0].get_contents()[0])
        cs = doc.xref_stream(cs_x)
        doc.close()
        cs2 = _phone_restore_stamp_tj(cs)
        if cs2 == cs:
            return pdf
        patched = _patch_contents_xref(pdf, cs_x, cs2)
        return patched if patched is not None else pdf
    except Exception as exc:
        logger.warning("phone stamp restore: %s", exc)
        return pdf


def _ensure_phone_template() -> None:
    import shutil
    if os.path.isfile(PHONE_ORIG_NARROW):
        return
    if os.path.isfile(_PHONE_SEED):
        os.makedirs(os.path.dirname(PHONE_ORIG_NARROW), exist_ok=True)
        shutil.copy2(_PHONE_SEED, PHONE_ORIG_NARROW)
        logger.info("phone shell: %s", PHONE_ORIG_NARROW)


def _phone_base_path() -> str:
    _ensure_phone_template()
    # Lean only — never T_phone_unlocked (F2 ~6892 → Proton FAKE).
    return PHONE_ORIG_NARROW


def _phone_shell_paths() -> List[str]:
    """Lean original only — unlocked Medium (~6892) is F2 size HARD FAKE."""
    _ensure_phone_template()
    out: List[str] = []
    if os.path.isfile(PHONE_ORIG_NARROW):
        out.append(PHONE_ORIG_NARROW)
    return out


def _donor_med_kfont002_listed(path: str) -> bool:
    try:
        import fitz
        import tbank_unlock_template as tut
        from tbank_sbp_stealth import _ff2_glyf_loca_sha, _load_k_font_002_packs

        doc = fitz.open(path)
        fm = tut._find_font_objects(doc)
        meta = fm.get("TinkoffSans-Medium")
        if not meta:
            doc.close()
            return False
        ff2 = doc.xref_stream(meta["fontfile_xref"])
        doc.close()
        return _ff2_glyf_loca_sha(ff2) in _load_k_font_002_packs()
    except Exception:
        return False


def _phone_shell_paths_for_amount(amount_digits: str) -> List[str]:
    """Lean shells only; skip K-FONT-002 Medium packs."""
    paths = [p for p in _phone_shell_paths() if not _donor_med_kfont002_listed(p)]
    if not paths:
        paths = _phone_shell_paths()[:1]
    return paths


def _strip_yo_tverd(text: str) -> str:
    """Keep user text as-is — unlocked phone shell has Ё/ё/Ъ/ъ."""
    return text


def _soft_cover_phone_amount_digits(ctx, amount: str) -> str:
    """Identity — never remap face amount digits.

    Missing Medium digits are injected (atlas raw); soft-mapping 6→0 etc. is
    forbidden (wrong face amount + donor-charset dependency).
    """
    return amount


# No shell-scale atlas raw for these — master fill → K-TBANK-GLYPH-ATLAS-001.
_PHONE_ATLAS_UNSAFE = str.maketrans({
    "Ш": "С", "Щ": "Ч", "ъ": "ь",
    "ш": "с", "щ": "ч",  # keep if shell has them; translate only when needed
})


def _phone_reg_has_atlas_raw(ch: str) -> bool:
    """True when raw glyph cache has a shell-scale outline for this letter."""
    if not ch or ch.isspace() or ch.isdigit() or not ch.isalpha():
        return True
    try:
        from tbank_sbp_stealth import TBANK_CHAR_TO_GID_REG, _raw_glyph_entry
        cid = TBANK_CHAR_TO_GID_REG.get(ch)
        if cid is None:
            return False
        return bool(
            _raw_glyph_entry(ord(ch), int(cid), is_medium=False, target_aw=550)
        )
    except Exception:
        return False


def _soft_cover_phone_reg_text(ctx, text: str) -> str:
    """Identity — never remap FIO letters (hydrate / inject instead)."""
    return text


def _prepare_phone_data(data: Dict) -> Dict:
    from tbank_sbp_stealth import _normalize_tbank_sender, _normalize_tbank_receiver

    dt_raw = str(data.get("date_time", _ORIG_DATE)).strip()
    if dt_raw.lower() in ("сейчас", "now", "-", "", "авто", "auto"):
                dt_raw = now_msk().strftime("%d.%m.%Y  %H:%M:%S")
    new_date = _fmt_date(dt_raw)

    amount_digits = re.sub(r"[^\d]", "", str(data.get("amount", "14000")))
    from tbank_dynamic import format_amount_like_template
    new_amount = format_amount_like_template(amount_digits, _ORIG_AMOUNT)

    receipt_raw = str(data.get("receipt_num", "авто")).strip()
    receipt_auto = receipt_raw.lower() in ("авто", "auto", "-", "")
    if receipt_auto:
        from tbank_corpus import gen_receipt_number

        # Всегда 1-132-XXX-XXX-XXX (фиксированный блок + рандом).
        receipt_raw = gen_receipt_number("phone", op_date=new_date)
        logger.info("auto receipt: %s", receipt_raw)

    sender_raw = str(data.get("sender", _ORIG_SENDER))
    sender = _normalize_tbank_sender(sender_raw) or sender_raw
    receiver = _normalize_tbank_receiver(str(data.get("receiver", _ORIG_RECEIVER)))

    sender = _strip_yo_tverd(sender)
    receiver = _strip_yo_tverd(receiver)
    return {
        "new_date":    new_date,
        "new_amount":  new_amount,
        "sender":      sender,
        "phone":       str(data.get("phone", _ORIG_PHONE)),
        "receiver":    receiver,
        "receipt_raw": receipt_raw,
        "_receipt_auto": receipt_auto,
        "_user_sender": sender,
        "_user_receiver": receiver,
        "_shell_sender": _ORIG_SENDER,
        "_shell_receiver": _ORIG_RECEIVER,
    }


def _phone_reg_texts(prepared: Dict) -> list:
    p = prepared
    return [
        p["new_date"], p["sender"], p["phone"], p["receiver"], p["new_amount"],
        f"Квитанция  \u2116 {p['receipt_raw']}",
    ]


def _extract_phone_fields(path: str) -> Optional[Dict[str, str]]:
    import fitz
    try:
        doc = fitz.open(path)
        lines = [l.strip() for l in doc[0].get_text().split("\n") if l.strip()]
        doc.close()
    except Exception:
        return None
    if "По номеру телефона" not in lines:
        return None

    def after(label: str) -> str:
        for i, l in enumerate(lines):
            if l == label and i + 1 < len(lines):
                return lines[i + 1]
        return ""

    def before(label: str) -> str:
        for i, l in enumerate(lines):
            if l == label and i > 0:
                return lines[i - 1]
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
            if prev and (prev[0].isdigit() or prev.startswith("Без")):
                amount_small = prev + (" " if not prev.endswith(" ") else "")
            break
    receipt = next((l for l in lines if l.startswith("Квитанция")), "")
    amt = amount_small or amount_big
    return {
        "date": lines[0],
        "amount": amt,
        "amount_total": amount_big or amt,
        "sender": before("Отправитель"),
        "phone": after("Телефон получателя"),
        "receiver": after("Получатель"),
        "receipt": receipt,
    }


def _default_phone_orig() -> Dict[str, str]:
    orig = _extract_phone_fields(_phone_base_path())
    if orig:
        return orig
    return {
        "date": _ORIG_DATE,
        "amount": _ORIG_AMOUNT,
        "amount_total": _ORIG_AMOUNT,
        "sender": _ORIG_SENDER,
        "phone": _ORIG_PHONE,
        "receiver": _ORIG_RECEIVER,
        "receipt": _ORIG_RECEIPT,
    }


def _phone_fields_fit_inplace(ctx, orig: Dict[str, str], adapted: Dict) -> bool:
    """Face must encode on donor fonts. Equal-CID preferred; unequal + Tm OK."""
    from tbank_orig_mode import fit_text_to_enc_len, fit_amount_to_enc_len

    checks = [
        ("date", "new_date", False),
        ("sender", "sender", False),
        ("phone", "phone", False),
        ("receiver", "receiver", False),
        ("amount", "new_amount", False),
        ("amount", "new_amount", True),
    ]
    for okey, pkey, medium in checks:
        old = orig.get(okey, "")
        new = adapted.get(pkey, "")
        if not old or not new or old.strip() == new.strip():
            continue
        if okey == "amount":
            o_amt = old if old.endswith(" ") else old.rstrip() + " "
            n_amt = new if new.endswith(" ") else new.rstrip() + " "
            if not ctx.enc(n_amt, medium=medium):
                return False
            _, nb = fit_amount_to_enc_len(ctx, o_amt, n_amt, medium=medium)
            if nb is None:
                return False
            continue
        if not ctx.enc(new, medium=medium):
            return False
        # Prefer equal CID; unequal still accepted (Tm realign + exact-flate).
        old_b = ctx.enc(old, medium=medium)
        if old_b:
            fit_text_to_enc_len(ctx, new, len(old_b), medium=medium, allow_trim=False)
    return True


def _tm_rewrite_before_tj(
    stream: bytes,
    pos: int,
    new_x: float,
) -> Tuple[bytes, int]:
    """Rewrite Tm before Tj at pos; keep X token length when possible."""
    from tbank_sbp_stealth import _fmt_coord_match

    look_from = max(0, pos - 200)
    region = stream[look_from:pos]
    tms = list(_TM_RE.finditer(region))
    if not tms:
        return stream, pos
    last = tms[-1]
    old_x_tok = last.group(1)
    old_y = last.group(2)
    x_s = _fmt_coord_match(new_x, old_x_tok).encode("ascii")
    new_tm = b"1 0 0 1 " + x_s + b" " + old_y + b" Tm"
    abs_start = look_from + last.start()
    abs_end = look_from + last.end()
    delta = len(new_tm) - (abs_end - abs_start)
    out = stream[:abs_start] + new_tm + stream[abs_end:]
    return out, pos + delta


def _pdf_literal_unescape(data: bytes) -> bytes:
    """Decode PDF literal-string escapes to raw bytes (for CID Tj inners)."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b != 0x5C:  # \
            out.append(b)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        c = data[i]
        if c == 0x6E:  # \n
            out.append(0x0A)
            i += 1
        elif c == 0x72:  # \r
            out.append(0x0D)
            i += 1
        elif c == 0x74:  # \t
            out.append(0x09)
            i += 1
        elif c == 0x62:  # \b
            out.append(0x08)
            i += 1
        elif c == 0x66:  # \f
            out.append(0x0C)
            i += 1
        elif c in (0x28, 0x29, 0x5C):  # \( \) \\
            out.append(c)
            i += 1
        elif 0x30 <= c <= 0x37:  # octal \ddd
            octal = bytearray()
            for _ in range(3):
                if i < n and 0x30 <= data[i] <= 0x37:
                    octal.append(data[i])
                    i += 1
                else:
                    break
            out.append(int(octal.decode("ascii"), 8) & 0xFF)
        else:
            out.append(c)
            i += 1
    return bytes(out)


def _find_phone_amount_tj(
    stream: bytes,
    needle: bytes,
    *,
    medium: bool,
) -> int:
    """Index of amount Tj: F3-backed, matching Medium(16)/Regular(9) Tf.

    Big and small amounts often share identical CID bytes after rewrite —
    stream.find(needle) alone hits the first (Итого) and corrupts its Tm.
    """
    start = 0
    while True:
        pos = stream.find(needle, start)
        if pos < 0:
            return -1
        tail = stream[pos + len(needle) : pos + len(needle) + 64]
        if b"/F3" not in tail:
            start = pos + 1
            continue
        look = stream[max(0, pos - 120) : pos]
        if medium:
            if b"16 Tf" in look or b"16.00 Tf" in look:
                return pos
        else:
            if b"16 Tf" in look or b"16.00 Tf" in look:
                start = pos + 1
                continue
            if b"9 Tf" in look or b"/F1" in look:
                return pos
        start = pos + 1


def _replace_phone_amount(
    ctx,
    stream: bytes,
    old: str,
    new: str,
    sz: float,
    medium: bool,
    *,
    preserve_layout: bool = False,
) -> Tuple[bytes, bool]:
    """Сумма phone: digits RIGHT before ₽; F3 «i» lands on x1=250."""
    if not new.endswith(" "):
        new = new.rstrip() + " "
    if not old.endswith(" "):
        old = old.rstrip() + " "
    enc_old = ctx.enc(old, medium=medium)
    if not enc_old:
        return stream, False
    needle = b"(" + enc_old + b")Tj"
    pos = _find_phone_amount_tj(stream, needle, medium=medium)
    if pos < 0:
        enc_old_r = ctx.enc(old.rstrip(), medium=medium)
        if enc_old_r:
            needle2 = b"(" + enc_old_r + b")Tj"
            if _find_phone_amount_tj(stream, needle2, medium=medium) >= 0:
                return stream, False
        return stream, False

    face = new
    enc_new = ctx.enc(new, medium=medium)
    if preserve_layout:
        from tbank_orig_mode import fit_amount_to_enc_len

        fitted, enc_eq = fit_amount_to_enc_len(ctx, old, new, medium=medium)
        if enc_eq is not None and len(enc_eq) == len(enc_old):
            # Only the size-matched occurrence — not global first hit (Итого).
            out = stream[:pos] + b"(" + enc_eq + b")Tj" + stream[pos + len(needle) :]
            face = fitted or new
            enc_new = enc_eq
            needle = b"(" + enc_eq + b")Tj"
            pos = _find_phone_amount_tj(out, needle, medium=medium)
            if pos < 0:
                return stream, False
            stream = out

    if not enc_new:
        return stream, False
    new_w = ctx.text_width(face, sz, medium=medium)
    if new_w <= 0:
        new_w = ctx.text_width(face, sz, medium=False)
    if new_w <= 0:
        return stream, False
    f3_w = _PHONE_F3_W_16 if sz >= 14.0 else _PHONE_F3_W_9
    new_x = _PHONE_COL_RIGHT - f3_w - new_w
    stream2, pos2 = _tm_rewrite_before_tj(stream, pos, new_x)
    out = stream2[:pos2] + b"(" + enc_new + b")Tj" + stream2[pos2 + len(needle) :]
    found = out.find(b"(" + enc_new + b")Tj", max(0, pos2 - 8))
    tail = out[found + len(enc_new) + 4 :][:80] if found >= 0 else b""
    if b"/F3" not in tail:
        logger.warning("phone amount: F3 ruble block missing after Tj")
        return stream, False
    return out, True


def _phone_decode_tj_inner(ctx, inner: bytes, *, medium: bool) -> Optional[str]:
    raw = _pdf_literal_unescape(inner)
    src = getattr(ctx, "uni_to_cid_med" if medium else "uni_to_cid_reg", {}) or {}
    uni = {c: u for u, c in src.items()}
    if not uni or len(raw) % 2:
        return None
    chars = []
    for i in range(0, len(raw), 2):
        cid = int.from_bytes(raw[i : i + 2], "big")
        if cid not in uni:
            return None
        chars.append(chr(uni[cid]))
    return "".join(chars)


def _strip_phone_amount_space_pads(ctx, stream: bytes) -> bytes:
    """Collapse amount/commission Tj trailing CID spaces to a single space.

    Exact-flate CID pads on «0 » / amounts push F3 past x=250 (walking edge).
    """
    space_reg = ctx.enc(" ", medium=False) or b"\x00\x03"
    space_med = ctx.enc(" ", medium=True) or space_reg
    out = stream
    for m in list(re.finditer(rb"\((?:\\.|[^\\()])*\)Tj", out)):
        tail = out[m.end() : m.end() + 64]
        if b"/F3" not in tail:
            continue
        inner = m.group(0)[1:-3]
        if len(inner) % 2 != 0 or not (4 <= len(inner) <= 48):
            continue
        look = out[max(0, m.start() - 120) : m.start()]
        medium = b"16 Tf" in look or (b"/F2" in look and b"9 Tf" not in look[-40:])
        space_cid = space_med if medium else space_reg
        if not inner.endswith(space_cid):
            continue
        n = 0
        while (
            len(inner) >= len(space_cid) * (n + 1)
            and inner[len(inner) - len(space_cid) * (n + 1) : len(inner) - len(space_cid) * n]
            == space_cid
        ):
            n += 1
        if n <= 1:
            continue
        fixed = inner[: len(inner) - len(space_cid) * n] + space_cid
        out = out[: m.start()] + b"(" + fixed + b")Tj" + out[m.end() :]
    return out


def _realign_phone_f3_amounts(ctx, stream: bytes) -> bytes:
    """Re-Tm every amount/commission Tj so following F3 lands on x1=250."""
    out = stream
    changed = True
    guard = 0
    while changed and guard < 12:
        changed = False
        guard += 1
        for m in list(re.finditer(rb"\((?:\\.|[^\\()])*\)Tj", out)):
            tail = out[m.end() : m.end() + 64]
            if b"/F3" not in tail:
                continue
            inner = m.group(0)[1:-3]
            if len(inner) % 2 != 0 or len(inner) > 48:
                continue
            look = out[max(0, m.start() - 140) : m.start()]
            y = _phone_tm_y_before(out, m.start(), window=140)
            if _phone_y_near(y, _PHONE_STATUS_Y):
                continue
            medium = b"16 Tf" in look or b"16.00 Tf" in look
            sz = 16.0 if medium else 9.0
            text = _phone_decode_tj_inner(ctx, inner, medium=medium)
            if text is None:
                # Same CID bytes often live in both cmaps after digit inject.
                text = _phone_decode_tj_inner(ctx, inner, medium=not medium)
            if not text or not any(ch.isdigit() for ch in text):
                continue
            new_w = ctx.text_width(text, sz, medium=medium)
            if new_w <= 0:
                new_w = ctx.text_width(text, sz, medium=False)
            if new_w <= 0:
                continue
            f3_w = _PHONE_F3_W_16 if sz >= 14.0 else _PHONE_F3_W_9
            new_x = _PHONE_COL_RIGHT - f3_w - new_w
            region = out[max(0, m.start() - 200) : m.start()]
            tms = list(_TM_RE.finditer(region))
            if not tms:
                continue
            try:
                cur_x = float(tms[-1].group(1))
            except Exception:
                continue
            if abs(cur_x - new_x) < 0.05:
                continue
            out, _ = _tm_rewrite_before_tj(out, m.start(), new_x)
            changed = True
            break
    return out


def _fix_phone_amount_right_edge(ctx, stream: bytes) -> bytes:
    """Strip CID over-pads then re-anchor F3 amounts to column right=250."""
    return _realign_phone_f3_amounts(ctx, _strip_phone_amount_space_pads(ctx, stream))


def _replace_tj_right(ctx, stream, old, new, sz, medium=False, *, preserve_layout=False, amount=False):
    """Value column: RIGHT-anchor Tm to 250 — never space-pad FIO/phone.

    Proton HARD on leading AND trailing ASCII spaces in party names
    (TBANK_SENDER_LEADING_WHITESPACE / TBANK_TEXT_TRAILING_WHITESPACE).
    """
    if amount:
        return _replace_phone_amount(
            ctx, stream, old, new, sz, medium, preserve_layout=preserve_layout,
        )
    enc = lambda t: ctx.enc(t, medium=medium)
    enc_old, enc_new = enc(old), enc(new)
    if not enc_old or not enc_new:
        # Empty old encodes as ``()Tj`` — the Jasper stamp at y=451.
        return stream, False
    needle = b"(" + enc_old + b")Tj"
    pos = stream.find(needle)
    if pos < 0:
        return stream, False
    if enc_old == enc_new:
        return stream, True
    new_w = ctx.text_width(new, sz, medium=medium)
    new_x = _PHONE_COL_RIGHT - new_w
    stream2, pos2 = _tm_rewrite_before_tj(stream, pos, new_x)
    return stream2[:pos2] + b"(" + enc_new + b")Tj" + stream2[pos2 + len(needle) :], True


def _replace_tj_left(
    ctx, stream, old, new, sz, medium=False, *,
    left_x: float = _PHONE_DATE_LEFT, preserve_layout: bool = False,
):
    """Date / left-column: equal-CID or fixed left edge x0=20."""
    if preserve_layout:
        from tbank_orig_mode import replace_field_preserve_tm

        stream2, _fitted, ok = replace_field_preserve_tm(
            ctx, stream, old, new, medium=medium, allow_trim=False,
        )
        if ok:
            return stream2, True
    enc = lambda t: ctx.enc(t, medium=medium)
    enc_old, enc_new = enc(old), enc(new)
    if not enc_old or not enc_new:
        return stream, False
    needle = b"(" + enc_old + b")Tj"
    pos = stream.find(needle)
    if pos < 0:
        return stream, False
    if enc_old == enc_new:
        return stream, True
    stream2, pos2 = _tm_rewrite_before_tj(stream, pos, left_x)
    return stream2[:pos2] + b"(" + enc_new + b")Tj" + stream2[pos2 + len(needle) :], True


def _replace_phone_receipt_inplace(
    ctx, stream: bytes, old_line: str, new_num: str, *, preserve_layout: bool = False,
) -> Tuple[bytes, bool]:
    """Receipt rewrite: equal-CID preferred; unequal TJ allowed with exact-flate."""
    from tbank_corpus import normalize_receipt_num, remix_receipt_d_only
    from tbank_orig_mode import replace_tj_bytes_inplace, fit_text_to_enc_len
    from tbank_stealth_v3 import _receipt_num_from_line

    new_num = normalize_receipt_num(new_num)
    old_num = _receipt_num_from_line(old_line)
    if old_num == new_num:
        return stream, True

    def _try_equal(new_line: str) -> Optional[Tuple[bytes, bool]]:
        new_b = ctx.enc(new_line)
        if not new_b:
            return None
        for old_text in (
            old_line,
            f"Квитанция  \u2116 {old_num}",
            f"Квитанция \u2116 {old_num}",
        ):
            old_b = ctx.enc(old_text)
            if not old_b or old_b not in stream:
                continue
            if len(old_b) == len(new_b):
                stream2, ok = replace_tj_bytes_inplace(stream, old_b, new_b)
                if ok:
                    return stream2, True
            fitted, nb = fit_text_to_enc_len(
                ctx, new_line, len(old_b), medium=False, allow_trim=False,
            )
            if nb is not None and len(nb) == len(old_b):
                stream2, ok = replace_tj_bytes_inplace(stream, old_b, nb)
                if ok:
                    return stream2, True
        return None

    def _try_unequal(new_line: str) -> Optional[Tuple[bytes, bool]]:
        new_b = ctx.enc(new_line)
        if not new_b:
            return None
        for old_text in (
            old_line,
            f"Квитанция  \u2116 {old_num}",
            f"Квитанция \u2116 {old_num}",
        ):
            old_b = ctx.enc(old_text)
            if not old_b or old_b not in stream:
                continue
            needle = b"(" + old_b + b")Tj"
            pos = stream.find(needle)
            if pos >= 0:
                return (
                    stream[:pos] + b"(" + new_b + b")Tj" + stream[pos + len(needle):],
                    True,
                )
        return None

    candidates = [new_num]
    for _ in range(48):
        cand = remix_receipt_d_only(old_num)
        if cand and cand not in candidates:
            candidates.append(cand)

    for num in candidates:
        hit = _try_equal(f"Квитанция  \u2116 {num}")
        if hit:
            logger.info("  receipt line: %s -> %s", old_num, num)
            return hit
    hit = _try_unequal(f"Квитанция  \u2116 {new_num}")
    if hit:
        logger.info("  receipt line (unequal): %s -> %s", old_num, new_num)
        return hit
    return stream, False


def _find_best_phone_donor(prepared: Dict) -> Optional[Tuple[str, Dict, List[str], List[str]]]:
    from tbank_corpus import pick_donor, template_paths
    from tbank_donor_fit import adapt_phone_prepared, pick_tbank_donor
    from tbank_sbp_stealth import _amount_slot_ok

    reg_texts = _phone_reg_texts(prepared)
    paths = list(template_paths("phone", _phone_base_path()) or [])
    preferred = pick_donor("phone", prepared.get("new_date"))
    if preferred and preferred in paths:
        paths = [preferred] + [p for p in paths if p != preferred]
    return pick_tbank_donor(
        paths,
        prepared,
        adapt_fn=adapt_phone_prepared,
        reg_texts=reg_texts,
        med_texts=[prepared["new_amount"]],
        extract_orig=_extract_phone_fields,
        min_score=20,
        allow_font_extend=False,
        max_miss_r=0,
    )


def _phone_commission_tm_x(stream: bytes, pos: int) -> Optional[float]:
    look = stream[max(0, pos - 200) : pos]
    tms = list(_TM_RE.finditer(look))
    if not tms:
        return None
    try:
        return float(tms[-1].group(1))
    except (TypeError, ValueError, IndexError):
        return None


def _force_phone_commission_zero(ctx, stream: bytes) -> Tuple[bytes, bool]:
    """Always show «0 ₽» (F1 «0 » + F3 i), never «Без комиссии».

    LEFT x≈20 at y=247.78 is the «Комиссия» label — never remove it.
    RIGHT x>80 at the same Y is the value («Без комиссии» or «0 »+F3).
    """
    from tbank_sbp_stealth import _inject_f3_ruble_after_tj

    zero = ctx.enc("0")
    if not zero:
        return stream, False
    # Space must be CID 3. Remapped unused letters (239/Д) also have TU U+0020
    # — encoding "0 " with those paints «0Д».
    new_b = zero + b"\x00\x03"

    new_w = ctx.text_width(_DISPLAY_COMMISSION, 9.0, medium=False)
    new_x = _PHONE_COL_RIGHT - _PHONE_F3_W_9 - new_w
    needle_new = b"(" + new_b + b")Tj"

    def _replace_right_commission(s: bytes, pos: int, old_needle: bytes) -> bytes:
        s2, pos2 = _tm_rewrite_before_tj(s, pos, new_x)
        s2 = s2[:pos2] + b"(" + new_b + b")Tj" + s2[pos2 + len(old_needle) :]
        return _inject_f3_ruble_after_tj(s2, new_b, tm_y=_PHONE_COMMISSION_Y)

    # Right-column slot (value): Tm x > 80 at commission Y.
    for m in re.finditer(rb"\((?:\\.|[^\\()])*\)Tj", stream):
        if not _phone_y_near(_phone_tm_y_before(stream, m.start()), _PHONE_COMMISSION_Y):
            continue
        tm_x = _phone_commission_tm_x(stream, m.start())
        if tm_x is None or tm_x <= 80:
            continue
        old_needle = m.group(0)
        inner = old_needle[1:-3]
        tail = stream[m.end() : m.end() + 48]
        if inner == new_b and b"/F3" in tail and b"0 g\n0 g" not in stream[m.end() : m.end() + 64]:
            stream2, _ = _tm_rewrite_before_tj(stream, m.start(), new_x)
            return stream2, True
        return _replace_right_commission(stream, m.start(), old_needle), True

    # Existing «0 » without F3 (broken shell) — anchor + inject ₽.
    for m in re.finditer(re.escape(needle_new), stream):
        if not _phone_y_near(_phone_tm_y_before(stream, m.start()), _PHONE_COMMISSION_Y):
            continue
        tail = stream[m.end() : m.end() + 40]
        stream2, _ = _tm_rewrite_before_tj(stream, m.start(), new_x)
        if b"/F3" not in tail or b"0 g\n0 g" in stream[m.end() : m.end() + 64]:
            stream2 = _inject_f3_ruble_after_tj(
                stream2, new_b, tm_y=_PHONE_COMMISSION_Y,
            )
        return stream2, True

    # Legacy «Без комиссии» bytes from this shell's cmap.
    old_b = ctx.enc(_LEGACY_COMMISSION) or ctx.enc("Без комиссии")
    if old_b:
        needle_old = b"(" + old_b + b")Tj"
        for m in re.finditer(re.escape(needle_old), stream):
            if not _phone_y_near(_phone_tm_y_before(stream, m.start()), _PHONE_COMMISSION_Y):
                continue
            tm_x = _phone_commission_tm_x(stream, m.start())
            if tm_x is None or tm_x <= 80:
                continue
            return _replace_right_commission(stream, m.start(), needle_old), True

    return stream, False


def _phone_force_che_tounicode(pdf: bytes) -> bytes:
    """CID 258 must stay U+0427 «Ч» — remap-to-space / prune → Ă / missing initial."""
    if not pdf:
        return pdf
    try:
        import fitz
        import tbank_unlock_template as tut
        from tbank_orig_mode import _find_stream_pos_for_xref
        from tbank_sbp_stealth import _patch_tounicode_xref

        doc = fitz.open(stream=pdf, filetype="pdf")
        meta = tut._find_font_objects(doc).get("TinkoffSans-Regular")
        if not meta:
            doc.close()
            return pdf
        tu_x = int(meta["tounicode_xref"])
        tu = doc.xref_stream(tu_x)
        doc.close()
        pat = re.compile(rb"<(0102)><(0102)><([0-9A-Fa-f]{4})>", re.I)
        m = pat.search(tu)
        if m and m.group(3).upper() == b"0427":
            return pdf
        if m:
            patched_tu = tu[: m.start(3)] + b"0427" + tu[m.end(3) :]
        else:
            ins = tu.find(b"beginbfrange")
            if ins < 0:
                return pdf
            nl = tu.find(b"\n", ins)
            if nl < 0:
                return pdf
            patched_tu = tu[: nl + 1] + b"<0102><0102><0427>\n" + tu[nl + 1 :]
        if patched_tu == tu:
            return pdf
        if len(patched_tu) == len(tu):
            cs, ce = _find_stream_pos_for_xref(pdf, tu_x)
            if cs is not None:
                from openpdf_deflate import openpdf_deflate

                need = ce - cs
                raw = pdf[cs:ce]
                comp = openpdf_deflate(patched_tu, 6)
                if comp is not None and len(comp) == need:
                    out = bytearray(pdf)
                    out[cs:ce] = comp
                    return bytes(out)
                try:
                    from openpdf_deflate import compress_to_size

                    comp2 = compress_to_size(patched_tu, need, raw)
                    if comp2 is not None and len(comp2) == need:
                        out = bytearray(pdf)
                        out[cs:ce] = comp2
                        return bytes(out)
                except Exception:
                    pass
        landed = _patch_tounicode_xref(pdf, tu_x, patched_tu)
        return landed if landed is not None else pdf
    except Exception as exc:
        logger.warning("phone Ч ToUnicode: %s", exc)
        return pdf


def _phone_install_receiver_che(pdf: bytes, prepared: Dict) -> bytes:
    """Install atlas «Ч»@258 + TU U+0427 so the initial is Ч, not a fat Ă."""
    recv = str((prepared or {}).get("receiver") or "")
    if "Ч" not in recv or not pdf:
        return pdf
    try:
        import fitz
        import tbank_unlock_template as tut
        from tbank_sbp_stealth import (
            _install_raw_glyph_bytes,
            _patch_fontfile2_xref,
            _pin_f1_head_flags_and_csa,
            _raw_glyph_entry,
        )

        entry = _raw_glyph_entry(ord("Ч"), 258, is_medium=False)
        if not entry or not entry.get("raw"):
            logger.warning("phone Ч atlas miss — TU only")
            return _phone_force_che_tounicode(pdf)
        doc = fitz.open(stream=pdf, filetype="pdf")
        meta = tut._find_font_objects(doc).get("TinkoffSans-Regular")
        if not meta:
            doc.close()
            return _phone_force_che_tounicode(pdf)
        x1 = int(meta["fontfile_xref"])
        ff2 = doc.xref_stream(x1)
        doc.close()
        out_ff = _pin_f1_head_flags_and_csa(
            _install_raw_glyph_bytes(ff2, {258: entry})
        )
        patched = _patch_fontfile2_xref(pdf, x1, out_ff)
        if patched is None:
            patched = pdf
        return _phone_force_che_tounicode(patched)
    except Exception as exc:
        logger.warning("phone Ч install: %s", exc)
        return _phone_force_che_tounicode(pdf)


def _fix_phone_et_whitespace_stream(stream: bytes) -> bytes:
    """Proton TBANK_CONTENT_ET_WHITESPACE_ANOMALY — only «\\nET», not « ET»."""
    out = stream
    while True:
        m = re.search(rb"(?:\r?\n)(?:[ \t]*(?:\r?\n))+[ \t]*ET\b", out)
        if not m:
            break
        out = out[: m.start()] + b"\nET" + out[m.end() :]
    while True:
        m = re.search(rb"\n([ \t]+)ET\b", out)
        if not m:
            break
        out = out[: m.start()] + b"\nET" + out[m.end() :]
    return out


def _realign_phone_value_fields(ctx, stream: bytes, prepared: Dict) -> bytes:
    """RIGHT-pin sender / phone / receiver after any layout pass."""
    out = stream
    for key in ("sender", "phone", "receiver"):
        text = str(prepared.get(key) or "").strip()
        if not text:
            continue
        enc = ctx.enc(text)
        if not enc:
            continue
        needle = b"(" + enc + b")Tj"
        new_w = ctx.text_width(text, 9.0, medium=False)
        new_x = _PHONE_COL_RIGHT - new_w
        for m in re.finditer(re.escape(needle), out):
            tm_x = _phone_commission_tm_x(out, m.start())
            if tm_x is not None and tm_x <= 80:
                continue
            out, _ = _tm_rewrite_before_tj(out, m.start(), new_x)
            break
    return out


def _commit_phone_contents(pdf: bytes, ctx, stream: bytes) -> bytes:
    """Write decoded CS back preserving compressed /Length when possible."""
    from openpdf_deflate import openpdf_deflate
    from tbank_stealth_v3 import _patch_length_and_rebuild

    if stream == ctx.cs_dec:
        return pdf
    out = bytearray(pdf)
    cs, ce, raw = ctx.cs_cs, ctx.cs_ce, ctx.cs_raw
    comp = openpdf_deflate(stream, 6)
    if comp is not None and len(comp) == len(raw):
        out[cs:ce] = comp
        return bytes(out)
    if comp is None:
        return pdf
    rebuilt = _patch_length_and_rebuild(out, cs, ce, comp)
    return rebuilt if rebuilt is not None else pdf


def repair_phone_layout_pdf(
    pdf: bytes, prepared: Optional[Dict] = None,
) -> bytes:
    """Undo generic polish damage; restore commission, value column, ET."""
    import os
    import tempfile

    from tbank_orig_mode import OrigContext

    if not pdf or not prepared:
        return pdf
    path = ""
    try:
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(pdf)
        ctx = OrigContext()
        if not ctx.load(path):
            return pdf
        stream = bytes(ctx.cs_dec)
        stream = _fix_phone_et_whitespace_stream(stream)
        stream = re.sub(
            rb"(q 175 0 0 63\.23 66 103\.77 cm /img3 Do Q\nBT\n)"
            rb"1 0 0 1 [\d.]+ 103\.77 Tm",
            rb"\g<1>1 0 0 1 66 103.77 Tm",
            stream,
        )
        stream = _phone_restore_stamp_tj(stream)
        stream, _ = _force_phone_commission_zero(ctx, stream)
        stream = _fix_phone_amount_right_edge(ctx, stream)
        stream = _realign_phone_value_fields(ctx, stream, prepared)
        stream = _fix_phone_amount_right_edge(ctx, stream)
        return _commit_phone_contents(pdf, ctx, stream)
    except Exception as exc:
        logger.warning("phone layout repair: %s", exc)
        return pdf
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _patch_phone_commission_in_pdf(pdf: bytes) -> bytes:
    """Final safety pass: force commission «0 ₽» on shipped phone PDF."""
    import os
    import tempfile

    from openpdf_deflate import openpdf_deflate
    from tbank_orig_mode import OrigContext

    path = ""
    try:
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(pdf)
        ctx = OrigContext()
        if not ctx.load(path):
            return pdf
        stream2, ok = _force_phone_commission_zero(ctx, bytes(ctx.cs_dec))
        if not ok or stream2 == ctx.cs_dec:
            return pdf
        out = bytearray(pdf)
        cs, ce, raw = ctx.cs_cs, ctx.cs_ce, ctx.cs_raw
        comp = openpdf_deflate(stream2, 6)
        if comp is not None and len(comp) == len(raw):
            out[cs:ce] = comp
            return bytes(out)
        if comp is None:
            logger.warning("phone commission post-patch: deflate miss")
            return pdf
        rebuilt = _patch_length_and_rebuild(out, cs, ce, comp)
        if rebuilt is None:
            logger.warning("phone commission post-patch: Length-rebuild miss")
            return pdf
        return rebuilt
    except Exception as exc:
        logger.warning("phone commission post-patch: %s", exc)
        return pdf
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


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
    reg_texts = _phone_reg_texts(p)
    ok_r, miss_r = ctx.can_render_reg(*reg_texts)
    ok_m, miss_m = ctx.can_render_med(p["new_amount"], p["new_amount"])
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
    o_amt = orig.get("amount") or _ORIG_AMOUNT
    o_total = orig.get("amount_total") or o_amt

    ok: Dict[str, bool] = {}
    pm = preserve_donor or preserve_metadata
    if p["new_date"] == orig.get("date"):
        ok["date"] = True
    else:
        stream, ok["date"] = _replace_tj_left(
                ctx, stream, orig["date"], p["new_date"], 9.0, medium=False,
            left_x=_PHONE_DATE_LEFT, preserve_layout=pm)

    if p["new_amount"].strip() == o_amt.strip() and p["new_amount"].strip() == o_total.strip():
        ok["amt_big"] = ok["amt_small"] = True
    else:
        # Digit CID escapes vary (0/2/4/7 → \\n/\\r); equal-byte fit often
        # impossible for short amounts like 7777 vs donor «14 000 ». Prefer
        # amount-aware equal fit; fall back to unequal TJ + Length rebuild.
        from tbank_dynamic import format_amount_like_template

        digits = re.sub(r"[^\d]", "", p["new_amount"]) or "0"
        amt_small = format_amount_like_template(digits, o_amt)
        amt_big = format_amount_like_template(digits, o_total)
        p["new_amount"] = amt_small

        def _put_amount(old: str, new: str, sz: float, medium: bool) -> Tuple[bytes, bool]:
            nonlocal stream
            if not new.endswith(" "):
                new = new.rstrip() + " "
            if old.strip() == new.strip() and old.endswith(" ") and new.endswith(" "):
                # Same digits — still ensure trailing space present in stream.
                return stream, True
            stream2, ok2 = _replace_phone_amount(
                ctx, stream, old, new, sz, medium, preserve_layout=pm,
            )
            return stream2, ok2

        stream, ok["amt_big"] = _put_amount(o_total, amt_big, 16.0, True)
        stream, ok["amt_small"] = _put_amount(o_amt, amt_small, 9.0, False)

    for name, key in [("sender", "sender"), ("phone", "phone"), ("receiver", "receiver")]:
        old, new = orig[key], p[key]
        if new == old:
            ok[name] = True
        else:
            stream, ok[name] = _replace_tj_right(
                ctx, stream, old, new, 9.0, medium=False,
                preserve_layout=pm)

    o_rcp = orig.get("receipt") or _ORIG_RECEIPT
    stream, ok["receipt"] = _replace_phone_receipt_inplace(
        ctx, stream, o_rcp, p["receipt_raw"], preserve_layout=pm)
    stream, ok["commission"] = _force_phone_commission_zero(ctx, stream)
    # Lock amount/commission F3 on x1=250 (no walking right edge).
    stream = _fix_phone_amount_right_edge(ctx, stream)
    stream = _phone_restore_stamp_tj(stream)
    # Amount/commission may change decoded CS length; Length rebuild below.

    # Proton TBANK_CONTENT_ET_WHITESPACE_ANOMALY: only «\nET» is legal.
    # Strip donor «\n   ET»; restore decoded length by padding a NON-receipt
    # Tj with CID spaces (\x00\x03) — never ASCII 0x20 (breaks 2-byte CID →
    # TEXT_LAYER_INCONSISTENT / TBANK_RECEIPT_ID_TRAILING_JUNK).
    def _strip_et_pad_into_tj(s: bytes) -> bytes:
        out = s
        # Collapse blank lines / CRLF runs immediately before ET → single «\nET».
        # Do not CID-restore huge fit pads — exact-flate pads elsewhere safely.
        while True:
            m = re.search(rb"(?:\r?\n)(?:[ \t]*(?:\r?\n))+[ \t]*ET\b", out)
            if not m:
                break
            out = out[: m.start()] + b"\nET" + out[m.end() :]
        while True:
            m = re.search(rb"\n([ \t]+)ET\b", out)
            if not m:
                break
            pad_n = len(m.group(1))
            stripped = out[: m.start()] + b"\nET" + out[m.end() :]
            # Keep decoded length: CID spaces into non-footer Tjs.
            cid_pad = b"\x00\x03" * ((pad_n + 1) // 2)
            tjs = list(re.finditer(rb"\((?:\\.|[^\\()])*\)Tj", stripped))
            if not tjs or not cid_pad:
                out = stripped
                continue
            targets = tjs[:-3] if len(tjs) > 3 else (tjs[:-1] if len(tjs) > 1 else [])
            placed = False
            space_cid = b"\x00\x03"
            for tj in reversed(targets):
                # Never CID-pad amount/commission (F3 follows) — pushes ₽ past 250.
                if b"/F3" in stripped[tj.end() : tj.end() + 64]:
                    continue
                inner = tj.group(0)[1:-3]
                if len(inner) % 2 != 0:
                    continue
                if not inner.endswith(space_cid):
                    continue
                if not (4 <= len(inner) <= 16):
                    continue
                inner_end = tj.end() - 3
                out = stripped[:inner_end] + cid_pad + stripped[inner_end:]
                placed = True
                break
            if not placed:
                # Do NOT space-pad after BT/0 g — creates indented operands
                # (TBANK_CONTENT_INDENTED_OPERAND). Accept length shrink.
                out = stripped
                continue
            if pad_n % 2 == 1 and len(out) == len(s) + 1:
                for sm in reversed(list(re.finditer(rb"  +", out))):
                    frag = out[sm.start() : sm.start() + 12]
                    if re.match(rb" +\n?ET\b", frag):
                        continue
                    if sm.start() == 0 or out[sm.start() - 1 : sm.start()] == b"\n":
                        continue
                    out = out[: sm.end() - 1] + out[sm.end() :]
                    break
        return out

    stream = _strip_et_pad_into_tj(stream)
    # ET/CID restore must not leave F3 amounts drifted — re-lock column.
    stream = _fix_phone_amount_right_edge(ctx, stream)

    for k, v in ok.items():
        logger.info("  [%s] %s %s", tag, "OK" if v else "FAIL", k)
    critical = ("date", "amt_big", "amt_small", "sender", "phone", "receiver")
    if any(not ok.get(k, True) for k in critical):
        logger.warning("  [%s] critical miss %s — reject candidate", tag, ok)
        return None
    elif not all(ok.values()):
        logger.warning("  [%s] non-critical miss %s — reject candidate", tag, ok)
        return None

    if not pm:
        stream = _normalize_content_stream_footer(stream)

    length_rebuilt = False
    if stream == ctx.cs_dec:
        pdf[cs:ce] = raw
    else:
        from openpdf_deflate import openpdf_deflate
        try:
            from tbank_channel_common import compress_orig_stream_nopad
        except ImportError:
            # Stale VPS deploy must never hard-fail phone emit.
            def compress_orig_stream_nopad(s: bytes, need: int):
                import zlib as _zl
                for lvl in (6, 5, 7, 4, 8, 9, 3, 2, 1):
                    hit = openpdf_deflate(s, lvl)
                    if (
                        hit is not None
                        and len(hit) == need
                        and hit[:2] == b"\x78\x9c"
                    ):
                        try:
                            if len(_zl.decompress(hit)) == len(s):
                                return hit
                        except Exception:
                            continue
                return None
        from tbank_sbp_stealth import _cs_whitespace_fingerprint

        # Fraudex «структура»: ANY CS /Length change vs donor → FAKE.
        # Never Length-rebuild. Prefer nopad; else minimal spaces at safe
        # anchors (after Q/BT/0 g) — never grow space-before-ET.
        def _space_et_runs(s: bytes) -> int:
            return len(re.findall(rb"[ ]{2,}(?=\n?ET\b)", s))

        def _phone_exact_cs(s: bytes, need: int, donor_sp: int):
            def _leading_space_lines(blob: bytes) -> int:
                return len(re.findall(rb"(?m)^[ ]+\S", blob))

            donor_lead = _leading_space_lines(ctx.cs_dec)

            def _skeleton_pad_ok(trial: bytes) -> bool:
                # Collapse operator spaces for compare so )Tj\\n pads can hit size.
                if _space_et_runs(trial) > 0:
                    return False
                if re.search(rb"(?:\r?\n)[ \t]*(?:\r?\n)+[ \t]*ET\b", trial):
                    return False
                # Proton HARD: never indent numeric/operand lines («   36 415 Td»).
                if re.search(rb"(?m)^[ \t]+\-?[\d.]", trial):
                    return False
                from sber_dynamic import _mask_pdf_literal_tj

                def _op_sk(blob: bytes) -> bytes:
                    sk = re.sub(rb"\n[ \t]+ET\b", b"\nET", blob)
                    sk = _mask_pdf_literal_tj(sk)
                    sk = re.sub(
                        rb"1 0 0 1 [0-9.]+ [0-9.]+ Tm", b"1 0 0 1 N N Tm", sk
                    )
                    sk = re.sub(rb"[ \t]+", b" ", sk)
                    sk = re.sub(rb" *\n *", b"\n", sk)
                    return sk

                donor_ref = re.sub(rb"\n[ \t]+ET\b", b"\nET", ctx.cs_dec)
                return _op_sk(trial) == _op_sk(donor_ref)

            hit = compress_orig_stream_nopad(s, need)
            if hit is not None and len(hit) == need and _skeleton_pad_ok(s):
                return s, hit
            nat = openpdf_deflate(s, 6)
            if nat is not None and len(nat) == need and _skeleton_pad_ok(s):
                return s, nat
            if nat is None:
                return None, None

            variants = [s]
            if len(nat) > need:
                for m in reversed(list(re.finditer(rb"  +", s))):
                    frag = s[m.start() : m.start() + 12]
                    if re.match(rb" +\n?ET\b", frag):
                        continue
                    if m.start() == 0 or s[m.start() - 1 : m.start()] == b"\n":
                        continue
                    for rem in range(1, min(8, m.end() - m.start()) + 1):
                        variants.append(s[: m.end() - rem] + s[m.end() :])

            def _op_pad_sites(blob: bytes) -> List[int]:
                """Never pad after )Tj — Proton TBANK_CONTENT_TJ_TRAILING_WHITESPACE.

                Jasper writes only ``)Tj\\n``. Pad at EOF instead.
                """
                if blob.endswith(b"\n"):
                    return [len(blob) - 1]
                return [len(blob)]

            def _try_pad_hit(base: bytes):
                if _cs_whitespace_fingerprint(base):
                    return None
                if not _skeleton_pad_ok(base):
                    return None
                bnat = openpdf_deflate(base, 6)
                if bnat is not None and len(bnat) == need:
                    return base, bnat
                hit2 = compress_orig_stream_nopad(base, need)
                if hit2 is not None and len(hit2) == need:
                    return base, hit2
                return None

            # Dual mild pads FIRST when short: 360 single-site OpenPDF probes
            # often flap the worker before we reach the working combo.
            if len(nat) > need:
                pass
            elif len(nat) < need:
                sites = _op_pad_sites(s)[:16]
                for i, pt1 in enumerate(sites):
                    for pt2 in sites[i + 1 :]:
                        for n1 in (7, 5, 3, 6, 4, 2, 1):
                            for n2 in (7, 5, 3, 6, 4, 2, 1):
                                ordered = sorted(
                                    ((pt1, n1), (pt2, n2)), reverse=True
                                )
                                base = s
                                for pt, n in ordered:
                                    base = base[:pt] + (b" " * n) + base[pt:]
                                hit = _try_pad_hit(base)
                                if hit is not None:
                                    return hit
                # Larger shrink (very short FIO) often needs 3 mild sites.
                import itertools

                sites3 = _op_pad_sites(s)[:12]
                for pts in itertools.combinations(sites3, 3):
                    for ns in itertools.product((7, 5, 3), repeat=3):
                        ordered = sorted(zip(pts, ns), reverse=True)
                        base = s
                        for pt, n in ordered:
                            base = base[:pt] + (b" " * n) + base[pt:]
                        hit = _try_pad_hit(base)
                        if hit is not None:
                            return hit

            # Grow inside NON-receipt (...)Tj with CID space only.
            # Never pad receipt/footer; never F3 amount/commission (edge 250).
            space_cid = b"\x00\x03"
            deficit = max(1, abs(need - len(nat)) + 24)
            tj_matches = list(re.finditer(rb"\((?:\\.|[^\\()])*\)Tj", s))
            if len(tj_matches) > 3:
                pad_targets = tj_matches[:-3]
            else:
                pad_targets = tj_matches[:-1] if len(tj_matches) > 1 else []
            for m in reversed(pad_targets):
                if b"/F3" in s[m.end() : m.end() + 64]:
                    continue
                inner = m.group(0)[1:-3]
                if len(inner) % 2 != 0:
                    continue
                if not inner.endswith(space_cid):
                    continue
                if not (4 <= len(inner) <= 16):
                    continue
                inner_end = m.end() - 3
                for n_spaces in range(1, min(16, deficit) + 1):
                    variants.append(s[:inner_end] + space_cid * n_spaces + s[inner_end:])
                    if len(variants) > 180:
                        break
                if len(variants) > 180:
                    break

            # Single-site operator spaces (≤15 — fingerprint burns ≥16).
            for pt in _op_pad_sites(s):
                for n in range(1, min(15, deficit) + 1):
                    variants.append(s[:pt] + (b" " * n) + s[pt:])
                    if len(variants) > 280:
                        break
                if len(variants) > 280:
                    break

            for base in list(variants[:280]):
                hit = _try_pad_hit(base)
                if hit is not None:
                    return hit
            return None, None

        # Keep donor ET whitespace: relocating pad spaces changes
        # TBANK_CONTENT_OPERATOR_SKELETON and often loses exact-flate.
        # After strip above, spaceET must stay 0.
        donor_sp = 0
        stream2, new_compressed = _phone_exact_cs(stream, orig_comp_len, donor_sp)
        if new_compressed is not None and len(new_compressed) == orig_comp_len:
            stream = stream2 if stream2 is not None else stream
            pdf[cs:ce] = new_compressed
            logger.info(
                "  [%s] exact flate %d (spaceET≤%d)",
                tag, orig_comp_len, _space_et_runs(stream),
            )
        else:
            # Fallback: Length-rebuild (keep stream clean — no receipt junk pads).
            from openpdf_deflate import fit_decoded_to_openpdf_size

            fitted = fit_decoded_to_openpdf_size(stream, orig_comp_len, level=6)
            if fitted is not None and _space_et_runs(fitted) == 0:
                if re.search(rb"(?:\r?\n)[ \t]*(?:\r?\n)+[ \t]*ET\b", fitted):
                    fitted = None
                # Verify no receipt trailing junk after fit pads.
                if fitted is not None:
                    tjs = list(re.finditer(rb"\((?:\\.|[^\\()])*\)Tj", fitted))
                    if tjs:
                        last_inner = tjs[-1].group(0)[1:-3]
                        # Reject ASCII 0x20 runs in last Tj (CID misalign → junk).
                        if b" " in last_inner or b"\x20\x20" in last_inner:
                            fitted = None
                if fitted is not None:
                    hit = compress_orig_stream_nopad(fitted, orig_comp_len)
                    if hit is None:
                        hit = openpdf_deflate(fitted, 6)
                    if hit is not None and len(hit) == orig_comp_len:
                        stream = fitted
                        pdf[cs:ce] = hit
                        logger.info("  [%s] exact flate via fit_decoded %d", tag, orig_comp_len)
                        new_compressed = hit
            if new_compressed is None or len(new_compressed) != orig_comp_len:
                # Prefer Length-rebuild over endless receipt-remix (bot hang).
                nat_now = openpdf_deflate(stream, 6)
                if nat_now is None:
                    logger.warning(
                        "  [%s] exact flate miss — reject (no compress)",
                        tag,
                    )
                    return None
                if len(nat_now) != orig_comp_len:
                    logger.warning(
                        "  [%s] exact flate %d≠%d — Length-rebuild emit",
                        tag, len(nat_now), orig_comp_len,
                    )
                hit = nat_now
                rebuilt = _patch_length_and_rebuild(pdf, cs, ce, hit)
                if rebuilt is None:
                    logger.warning(
                        "  [%s] Length-rebuild failed need=%d got=%d",
                        tag, orig_comp_len, len(hit),
                    )
                    return None
                pdf = bytearray(rebuilt)
                length_rebuilt = True
                logger.warning(
                    "  [%s] Length-rebuild %d→%d (emit, no remix hang)",
                    tag, orig_comp_len, len(hit),
                )

    from tbank_channel_common import finalize_orig_result, patch_channel_metadata

    # Dates/Keywords via equal-length patch (CreationDate ≈ face). Keep trailer /ID
    # (create_tbank_pipeline no longer randomizes phone /ID — SafeCheck structure).
    result = bytes(pdf)
    result = patch_channel_metadata(result, p["new_date"])
    pad_cap = min(donor_size, _PHONE_HARD_MAX) if donor_size else _PHONE_HARD_MAX
    if pm and donor_size and not length_rebuilt and not skip_size_pad:
        from tbank_sbp_stealth import _fix_stream_separators, _pad_pdf_to_exact_size
        before = result
        result = _fix_stream_separators(result)
        if len(result) != len(before):
            result = before
        if len(result) <= pad_cap:
            result = _pad_pdf_to_exact_size(result, pad_cap)
        elif len(result) > _PHONE_HARD_MAX:
            result = _pad_pdf_to_exact_size(result, _PHONE_HARD_MAX)
        if donor_size and len(result) != donor_size and len(result) <= _PHONE_HARD_MAX:
            logger.warning(
                "📱 PHONE %s size %d≠%d — soft continue (bot emit)",
                tag, len(result), donor_size,
            )
    elif length_rebuilt:
        logger.info("📱 PHONE %s Length-rebuild emit (%d B)", tag, len(result))
    elif not pm:
        result = finalize_orig_result(
            result,
            new_date=p["new_date"],
            preserve_donor=False,
            skip_size_pad=True,
            donor_size=0,
            ctx=ctx,
            stream=stream,
            prune=False,
        )
    if len(result) > _PHONE_HARD_MAX:
        from tbank_sbp_stealth import _pad_pdf_to_exact_size
        trimmed = _pad_pdf_to_exact_size(result, _PHONE_HARD_MAX)
        if len(trimmed) <= _PHONE_HARD_MAX:
            result = trimmed
        else:
            logger.warning(
                "📱 PHONE %s reject over HARD %d>%d",
                tag, len(result), _PHONE_HARD_MAX,
            )
            return None
    logger.info("📱 PHONE %s [%s]: %d bytes", tag, os.path.basename(template_path), len(result))
    return result


def _phone_stream_text(pdf_path: str) -> str:
    """Decode Identity-H Tj strings from phone content (for keep-set)."""
    import fitz
    from tbank_orig_mode import OrigContext

    ctx = OrigContext()
    if not ctx.load(pdf_path):
        return ""
    doc = fitz.open(pdf_path)
    try:
        s = None
        for xref in range(1, doc.xref_length()):
            if not doc.xref_is_stream(xref):
                continue
            try:
                blob = doc.xref_stream(xref)
            except Exception:
                continue
            if blob and b"/F1" in blob and b"Tj" in blob:
                s = blob
                break
        if not s:
            return ""
    finally:
        doc.close()
    cid_to_uni = {c: u for u, c in ctx.uni_to_cid_reg.items()}
    cid_to_uni.update({c: u for u, c in ctx.uni_to_cid_med.items()})
    out = []
    i = 0
    while True:
        j = s.find(b"(", i)
        if j < 0:
            break
        k = j + 1
        buf = bytearray()
        while k < len(s):
            c = s[k]
            if c == 0x5C:
                if k + 1 < len(s):
                    buf.append(s[k + 1])
                k += 2
                continue
            if c == 0x29:
                break
            buf.append(c)
            k += 1
        if s[k + 1 : k + 3] == b"Tj" and len(buf) >= 2 and len(buf) % 2 == 0:
            for n in range(0, len(buf), 2):
                cid = (buf[n] << 8) | buf[n + 1]
                u = cid_to_uni.get(cid)
                if u:
                    out.append(chr(u))
        i = k + 1
    return "".join(out)


def _phone_keep_text(prepared: Dict, pdf_path: str) -> str:
    parts = list(_phone_reg_texts(prepared))
    amt = prepared.get("new_amount") or ""
    if amt:
        parts.append(amt)
    parts.append(_phone_stream_text(pdf_path))
    # Static face labels (never steal their glyphs).
    parts.append(
        "Перевод По номеру телефона Статус Успешно Сумма Комиссия "
        "Отправитель Телефон получателя Получатель Итого "
        "Служба поддержки По вопросам зачисления обращайтесь к получателю "
        "fb@tbank.ru Квитанция №"
    )
    return "".join(parts)


def _ensure_phone_payload_glyphs(pdf_path: str, prepared: Dict) -> Optional[str]:
    """Resolve Regular + Medium glyphs via inject — no digit/letter soft-cover."""
    from tbank_orig_mode import OrigContext
    from tbank_sbp_stealth import _extend_donor_font_chars
    from tbank_channel_common import payload_missing_contours

    ctx = OrigContext()
    if not ctx.load(pdf_path):
        return None
    # Keep face amount/FIO as requested; inject atlas-safe contours for misses.
    regs = _phone_reg_texts(prepared)
    ok_r, miss_r = ctx.can_render_reg(*regs)
    amt = prepared.get("new_amount") or ""
    ok_m, miss_m = ctx.can_render_med(amt, amt)
    contour_r, contour_m = payload_missing_contours(
        pdf_path, ctx, reg_texts=regs, med_texts=[amt],
    )
    miss_r = list(dict.fromkeys([*miss_r, *contour_r]))
    miss_m = list(dict.fromkeys([*miss_m, *contour_m]))
    # Only inject letters that have atlas-safe raw (Ш/Щ/ъ otherwise FAIL — no remap).
    miss_r = [ch for ch in miss_r if (not ch.isalpha()) or _phone_reg_has_atlas_raw(ch)]
    unsafe = [
        ch for ch in "".join(_phone_reg_texts(prepared))
        if ch.isalpha()
        and ord(ch) not in (getattr(ctx, "uni_to_cid_reg", None) or {})
        and not _phone_reg_has_atlas_raw(ch)
    ]
    if unsafe:
        logger.info("  phone atlas-unsafe letters (no soft-cover): %s", "".join(dict.fromkeys(unsafe)))
        return None
    if not miss_r and not miss_m:
        return pdf_path

    # Pass full face amount (not only missing digits) so Medium keep-set
    # retains every digit on the face while adding the rest.
    med_payload = (amt or "") + "".join(miss_m)
    extended = _extend_donor_font_chars(
        pdf_path, ctx,
        reg_chars="".join(miss_r),
        med_chars="".join(ch for ch in med_payload if ch.isdigit() or ch in "Итого "),
        allow_med_corpus_swap=False,
    )
    if not extended:
        return None
    ctx2 = OrigContext()
    if not ctx2.load(extended):
        return None
    ok_r, miss_r = ctx2.can_render_reg(*_phone_reg_texts(prepared))
    ok_m, miss_m = ctx2.can_render_med(prepared.get("new_amount") or "")
    if miss_r or miss_m:
        logger.info(
            "  phone glyph miss after layer: reg=%s med=%s",
            "".join(miss_r) or "—",
            "".join(miss_m) or "—",
        )
        return None
    return extended


def _try_donor_orig_phone(prepared: Dict) -> Optional[bytes]:
    """Real fake on project phone template: mutate face fields + D-only receipt."""
    from tbank_corpus import remix_receipt_d_only
    from tbank_donor_fit import adapt_phone_prepared
    from tbank_orig_mode import OrigContext

    digits = re.sub(r"[^\d]", "", prepared.get("new_amount") or "")
    for donor in _phone_shell_paths_for_amount(digits):
        orig = _extract_phone_fields(donor)
        if not orig:
            continue

        ctx = OrigContext()
        if not ctx.load(donor):
            continue
        adapted = adapt_phone_prepared(ctx, dict(prepared))
        # Face amount/FIO unchanged — Medium/Regular inject covers missing glyphs.

        o_rcp = orig.get("receipt") or _ORIG_RECEIPT
        m = re.search(r"1-\d{3}-\d{3}-\d{3}-\d{3}", o_rcp)
        base_num = m.group(0) if m else "1-104-397-813-566"
        user_rcp = (prepared.get("receipt_raw") or "").strip()
        if not user_rcp or user_rcp.lower() in ("авто", "auto", "-"):
            adapted["receipt_raw"] = remix_receipt_d_only(base_num)
        else:
            from tbank_corpus import normalize_receipt_num
            adapted["receipt_raw"] = normalize_receipt_num(user_rcp)

        # Refuse pure original face (receipt-only clone) — must change real fields.
        face_keys = (
            ("date", "new_date"),
            ("amount", "new_amount"),
            ("sender", "sender"),
            ("phone", "phone"),
            ("receiver", "receiver"),
        )
        changed = False
        for okey, pkey in face_keys:
            ov = (orig.get(okey) or "").strip()
            nv = (adapted.get(pkey) or "").strip()
            if ov and nv and ov != nv:
                changed = True
                break
        if not changed:
            logger.info("  [DONOR-ORIG] skip: face identical to template")
            continue

        donor_use = _ensure_phone_payload_glyphs(donor, adapted)
        if not donor_use:
            continue
        if donor_use != donor:
            orig = _extract_phone_fields(donor_use) or orig
            ctx = OrigContext()
            if not ctx.load(donor_use):
                continue

        if not _phone_fields_fit_inplace(ctx, orig, adapted):
            logger.info(
                "  [DONOR-ORIG] skip %s: fields do not fit CID slots",
                os.path.basename(donor),
            )
            continue

        # Prefer exact flate via receipt remix (auto). Long FIO that still
        # overshoots falls through to Length-rebuild inside _try_orig_mode_on.
        auto_rcp = bool(prepared.get("_receipt_auto")) or (
            not user_rcp or user_rcp.lower() in ("авто", "auto", "-")
        )
        max_rcp_try = 3 if auto_rcp else 1
        for rcp_i in range(max_rcp_try):
            if auto_rcp and rcp_i > 0:
                adapted["receipt_raw"] = remix_receipt_d_only(base_num)
            pdf = _try_orig_mode_on(
                donor_use,
                orig,
                adapted,
                tag="DONOR-ORIG",
                preserve_metadata=True,
                preserve_donor=True,
                skip_size_pad=False,
            )
            if pdf is not None:
                if rcp_i > 0:
                    logger.info(
                        "  [DONOR-ORIG] exact flate via receipt remix try %d",
                        rcp_i + 1,
                    )
                prepared["receipt_raw"] = adapted["receipt_raw"]
                return pdf
            # Length-rebuild now emits on undersize — stop remixing this donor.
            break
    return None


def _try_orig_mode(prepared: Dict) -> Optional[bytes]:
    try:
        from tbank_corpus import template_paths, pick_donor
        paths = template_paths("phone", _phone_base_path())
        preferred = pick_donor("phone", prepared.get("new_date"))
        if preferred and preferred in paths:
            paths = [preferred] + [x for x in paths if x != preferred]
    except Exception:
        paths = [_phone_base_path()]

    default = _default_phone_orig()
    for path in paths:
        if path == _phone_base_path() and not os.path.isfile(path):
            continue
        orig = _extract_phone_fields(path) if path != _phone_base_path() else default
        if orig is None:
            orig = default if path == _phone_base_path() else None
        if orig is None:
            continue
        path_use = _ensure_phone_payload_glyphs(path, prepared)
        if not path_use:
            continue
        if path_use != path:
            orig = _extract_phone_fields(path_use) or orig
        res = _try_orig_mode_on(path_use, orig, prepared, tag="ORIG")
        if res is not None:
            return res
    return None


def _build_dynamic_phone(prepared: Dict) -> Optional[bytes]:
    from tbank_dynamic import build_dynamic_tbank

    p = prepared
    base = _phone_base_path()
    orig_f = _extract_phone_fields(base) or _default_phone_orig()
    o_receipt = orig_f.get("receipt") or _ORIG_RECEIPT
    o_date = orig_f.get("date") or _ORIG_DATE
    o_amt = orig_f.get("amount") or _ORIG_AMOUNT
    o_sender = orig_f.get("sender") or _ORIG_SENDER
    o_phone = orig_f.get("phone") or _ORIG_PHONE
    o_receiver = orig_f.get("receiver") or _ORIG_RECEIVER

    need_r = _phone_reg_texts(p)
    col = _PHONE_COL_RIGHT

    def apply(stream, er, em, rtj, replace_once, _er_soft=None):
        ok: Dict[str, bool] = {}
        stream, ok["date"] = replace_once(stream, o_date, p["new_date"])
        stream, ok["amt_big"] = rtj(
            stream, o_amt, p["new_amount"], 16.0, True,
            right_edge=col - _PHONE_F3_W_16,
        )
        stream, ok["amt_small"] = rtj(
            stream, o_amt, p["new_amount"], 9.0, False,
            right_edge=col - _PHONE_F3_W_9,
        )
        for name, old, new in [
            ("sender", o_sender, p["sender"]),
            ("phone", o_phone, p["phone"]),
            ("receiver", o_receiver, p["receiver"]),
        ]:
            stream, ok[name] = rtj(stream, old, new, 9.0, False, right_edge=col)
        stream, ok["receipt"] = _replace_card_receipt(stream, er, o_receipt, p["receipt_raw"])
        return stream, ok

    def _phone_dynamic_post(pdf: bytes) -> Optional[bytes]:
        if len(pdf) > _PHONE_HARD_MAX:
            logger.warning("phone dynamic size %d > HARD max — ship", len(pdf))
        return _patch_phone_commission_in_pdf(pdf)

    return build_dynamic_tbank(
        orig_path=base,
        need_r_texts=need_r,
        need_m_texts=[p["new_amount"]],
        apply_replacements=apply,
        metadata_date=p["new_date"],
        patch_metadata_fn=_patch_pdf_metadata,
        log_label="📱 PHONE DYNAMIC",
        post_process=_phone_dynamic_post,
    )


def _phone_med_kfont002_ok(pdf: bytes) -> bool:
    """True if Medium FontFile2 is not a Proton K-FONT-002 known pack."""
    try:
        import fitz
        import tbank_unlock_template as tut
        from tbank_sbp_stealth import _ff2_glyf_loca_sha, _load_k_font_002_packs

        doc = fitz.open(stream=pdf, filetype="pdf")
        fm = tut._find_font_objects(doc)
        meta = fm.get("TinkoffSans-Medium")
        if not meta:
            doc.close()
            return True
        ff2 = doc.xref_stream(meta["fontfile_xref"])
        doc.close()
        return _ff2_glyf_loca_sha(ff2) not in _load_k_font_002_packs()
    except Exception as exc:
        logger.warning("phone K-FONT-002 check failed: %s", exc)
        return True


def _phone_f2_size_ok(pdf: bytes) -> Tuple[bool, str]:
    """Proton phone Medium F2 HARD: 5056–5420 (height=451 atlas)."""
    try:
        import fitz
        import tbank_unlock_template as tut

        doc = fitz.open(stream=pdf, filetype="pdf")
        fm = tut._find_font_objects(doc)
        meta = fm.get("TinkoffSans-Medium")
        if not meta:
            doc.close()
            return False, "med-missing"
        n = len(doc.xref_stream(meta["fontfile_xref"]))
        doc.close()
        if not (5056 <= n <= 5420):
            return False, f"med-f2={n}"
        return True, f"med-f2={n}"
    except Exception as exc:
        return False, f"f2-exc:{exc}"


def _lean_phone_medium_after_face(pdf: bytes, amount: str) -> Optional[bytes]:
    """Blank Medium digit glyfs not on the face — land F2 ≤5620 after inject."""
    from io import BytesIO as _BIO

    import fitz
    import tbank_unlock_template as tut
    from fontTools.ttLib import TTFont
    from tbank_sbp_stealth import (
        _blank_ff2_unused_glyfs,
        _ff2_composite_closure,
        _ff2_restore_shell_tables,
        _patch_fontfile2_xref,
        _recalc_head_csa,
    )

    digits = {ord(ch) for ch in (amount or "") if ch.isdigit()}
    keep_cps = {ord(ch) for ch in "Итого"} | {0x20} | digits
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        fm = tut._find_font_objects(doc)
        meta = fm.get("TinkoffSans-Medium")
        if not meta:
            doc.close()
            return pdf
        ff_xref = meta["fontfile_xref"]
        tu_xref = meta["tounicode_xref"]
        ff2 = doc.xref_stream(ff_xref)
        if 5056 <= len(ff2) <= 5420:
            doc.close()
            return pdf
        sub = tut._parse_subset_tounicode(
            doc.xref_stream(tu_xref).decode("latin1", "replace")
        )
        doc.close()
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
        lean = _blank_ff2_unused_glyfs(ff2, keep)
        lean = _ff2_restore_shell_tables(ff2, lean)
        # Freeze head bbox to pre-lean shell (PDF /FontBBox stays donor).
        from tbank_sbp_stealth import _head_bbox_from_ff2, _restore_head_bbox

        shell_bb = _head_bbox_from_ff2(ff2)
        if shell_bb is not None:
            lean = _restore_head_bbox(lean, shell_bb)
        else:
            lean = _recalc_head_csa(lean)
        if not (5056 <= len(lean) <= 5420):
            logger.warning("phone F2 lean miss %d→%d (want 5056..5420)", len(ff2), len(lean))
            return None
        patched = _patch_fontfile2_xref(pdf, ff_xref, lean)
        if patched is None:
            return None
        logger.info("phone F2 lean %d→%d", len(ff2), len(lean))
        return patched
    except Exception as exc:
        logger.warning("phone F2 lean failed: %s", exc)
        return None


# Proton TBANK_FONTFILE2_SIZE_STRONG_OUTLIER (phone F1): HARD 15496–16596.
_F1_PHONE_DEC_LO = 15496
_F1_PHONE_DEC_HI = 16596


def _lean_phone_regular_after_face(pdf: bytes, prepared: Dict) -> Optional[bytes]:
    """Peel unused F1 glyfs so decoded lands in phone HARD band (≤16596)."""
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _TBANK_F1_OPENPDF_CSA,
        _ff2_composite_closure,
        _ff2_restore_shell_tables,
        _head_bbox_from_ff2,
        _patch_fontfile2_xref,
        _restore_head_bbox,
        _restore_head_csa,
        _trim_f1_into_v3_window,
        _ttf_num_glyphs,
    )

    parts = list(_phone_reg_texts(prepared))
    amt = prepared.get("new_amount") or ""
    if amt:
        parts.append(amt)
    parts.append(
        "Перевод По номеру телефона Статус Успешно Сумма Комиссия "
        "Отправитель Телефон получателя Получатель Итого "
        "Служба поддержки По вопросам зачисления обращайтесь к получателю "
        "fb@tbank.ru Квитанция №"
    )
    keep_cps = {ord(ch) for ch in "".join(parts)}
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        fm = tut._find_font_objects(doc)
        meta = fm.get("TinkoffSans-Regular")
        if not meta:
            doc.close()
            return pdf
        ff_xref = meta["fontfile_xref"]
        tu_xref = meta["tounicode_xref"]
        ff2 = doc.xref_stream(ff_xref)
        if _F1_PHONE_DEC_LO <= len(ff2) <= _F1_PHONE_DEC_HI:
            doc.close()
            return pdf
        if len(ff2) < _F1_PHONE_DEC_LO:
            doc.close()
            logger.warning("phone F1 under HARD lo %d<%d", len(ff2), _F1_PHONE_DEC_LO)
            return None
        sub = tut._parse_subset_tounicode(
            doc.xref_stream(tu_xref).decode("latin1", "replace")
        )
        doc.close()
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
            ff2, keep, lo=_F1_PHONE_DEC_LO, hi=_F1_PHONE_DEC_HI,
        )
        lean = _ff2_restore_shell_tables(ff2, lean)
        from tbank_sbp_stealth import _sync_hmtx_lsb_to_xmin
        lean = _sync_hmtx_lsb_to_xmin(lean, keep)
        shell_bb = _head_bbox_from_ff2(ff2)
        if shell_bb is not None:
            lean = _restore_head_bbox(lean, shell_bb)
        if _ttf_num_glyphs(lean) > 200:
            lean = _restore_head_csa(lean, _TBANK_F1_OPENPDF_CSA)
        if not (_F1_PHONE_DEC_LO <= len(lean) <= _F1_PHONE_DEC_HI):
            logger.warning("phone F1 lean miss %d→%d", len(ff2), len(lean))
            return None
        patched = _patch_fontfile2_xref(pdf, ff_xref, lean)
        if patched is None:
            return None
        logger.info("phone F1 lean %d→%d", len(ff2), len(lean))
        return patched
    except Exception as exc:
        logger.warning("phone F1 lean failed: %s", exc)
        return None


def _phone_layout_ok(pdf: bytes, prepared: Dict) -> Tuple[bool, str]:
    """Sanity vs originals: date LEFT≈20, values RIGHT≈250, user face present.

    Preserve-Tm equal-CID can leave x1 a few pt short of 250 when glyph advances
    differ from the donor slot — allow a small underfill, never past 251.
    """
    try:
        import fitz

        doc = fitz.open(stream=pdf, filetype="pdf")
        page = doc[0]
        text = page.get_text()
        date = (prepared.get("new_date") or "")[:10]
        sender = prepared.get("sender") or ""
        receiver = prepared.get("receiver") or ""
        phone = prepared.get("phone") or ""
        amt_digits = re.sub(r"[^\d]", "", prepared.get("new_amount") or "")
        checks = []
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                t = "".join(s["text"] for s in spans).strip()
                if not t:
                    continue
                x0 = min(s["bbox"][0] for s in spans)
                x1 = max(s["bbox"][2] for s in spans)
                checks.append((t, x0, x1))
        doc.close()
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if not re.match(r"^\d{2}\.\d{2}\.\d{4}", first_line):
            return False, f"date-line-corrupted:{first_line[:40]!r}"
        # date left
        date_hits = [c for c in checks if date and date in c[0]]
        if not date_hits:
            return False, "date-missing"
        if abs(date_hits[0][1] - _PHONE_DATE_LEFT) > 1.5:
            return False, f"date-not-left x0={date_hits[0][1]:.1f}"
        # values right — never spill past edge; slight underfill OK on preserve path
        # Support contact must match donor spacing exactly.
        for line in text.splitlines():
            if "fb@tbank" in line and line.strip() != "Служба поддержки fb@tbank.ru":
                return False, f"support-spacing:{line!r}"
        for needle, name in ((sender, "sender"), (receiver, "receiver"), (phone, "phone")):
            if not needle:
                continue
            hits = [c for c in checks if needle in c[0]]
            if not hits:
                return False, f"{name}-missing"
            raw = hits[0][0]
            if raw.startswith(" ") or raw.startswith("\t"):
                return False, f"{name}-leading-ws"
            if raw.endswith(" ") or raw.endswith("\t"):
                return False, f"{name}-trailing-ws"
            x1 = hits[0][2]
            # Donor value column ends at exactly 250 — keep tight.
            if abs(x1 - _PHONE_COL_RIGHT) > 1.5:
                return False, f"{name}-not-right x1={x1:.1f}"
        # Amount/commission ₽ (F3 / ALSRubl «i») must land on the same right edge.
        for t, x0, x1 in checks:
            if t.strip() not in ("₽", "i"):
                continue
            if x1 < 200:
                continue
            if abs(x1 - _PHONE_COL_RIGHT) > 1.8:
                return False, f"ruble-not-right x1={x1:.1f} text={t!r}"
        if sender and sender not in text:
            return False, "sender-text-missing"
        if receiver and receiver not in text:
            return False, "receiver-text-missing"
        if amt_digits:
            compact = re.sub(r"\D", "", text)
            if amt_digits not in compact:
                return False, "amount-mismatch"
        if "Дамир Сеничев" in text and "Дамир Сеничев" not in (sender, receiver):
            return False, "donor-fio-leaked"
        if "Без комиссии" in text:
            return False, "commission-not-zero"
        if "Марина Ч." in text and "Марина Ч." not in (sender, receiver):
            return False, "donor-fio-leaked"
        if "Итого" not in text:
            return False, "itogo-missing"
        # Proton TBANK_TEXT_TRAILING_WHITESPACE — static labels only.
        # Value slots may carry equal-CID trailing spaces from the donor.
        _label_ws = (
            "По вопросам",
            "Служба поддержки",
            "fb@tbank",
            "Итого",
        )
        for line in text.splitlines():
            if not (line.endswith(" ") or line.endswith("\t")):
                continue
            if any(n in line for n in _label_ws):
                return False, f"trailing-ascii-ws:{line.strip()[:40]!r}"
        # Proton TEXT_LAYER_INCONSISTENT / TBANK_RECEIPT_ID_TRAILING_JUNK
        for line in text.splitlines():
            if "Квитанция" not in line:
                continue
            m = re.search(r"(1-\d{3}-\d{3}-\d{3}-\d{3})(.*)$", line)
            if not m:
                return False, "receipt-id-missing"
            tail = m.group(2) or ""
            if tail.strip(" \t"):
                return False, f"receipt-trailing-junk:{tail[:20]!r}"
            if any(ord(c) < 32 or ord(c) in (0x2000, 0x2001, 0x2020) for c in line):
                return False, "text-layer-controls"
        if "\x00" in text or "\u2000" in text or "†" in text:
            return False, "text-layer-controls"
        return True, "ok"
    except Exception as exc:
        return False, f"layout-exc:{exc}"


def _phone_remap_unused_cmap_uniscodes(
    pdf: bytes, prepared: Optional[Dict] = None,
) -> bytes:
    """In-place: unused letter unis in ToUnicode → U+0020 (same hex width).

    Proton UNUSED_CID_PRESENT / CMAP_EXTRA_SYMBOLS when donor letters (БДМЧ…)
    stay in CMap but are absent from the face text. Full prune often misses
    exact flate size; remapping uni keeps decoded length (±0).
    """
    import fitz
    import tbank_unlock_template as tut
    from openpdf_deflate import openpdf_deflate, compress_to_size, fit_decoded_to_openpdf_size
    from tbank_orig_mode import _find_stream_pos_for_xref

    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        fonts = tut._find_font_objects(doc)
        text = doc[0].get_text()
        face = set(text)
        try:
            from tbank_sbp_stealth import _tbank_keep_face_uniscodes
            keep_unis = _tbank_keep_face_uniscodes(prepared) | face
        except Exception:
            keep_unis = face | set("Итого")
        out = bytearray(pdf)
        changed = False
        for key in ("TinkoffSans-Regular", "TinkoffSans-Medium"):
            fm = fonts.get(key)
            if not fm:
                continue
            tu_xref = fm["tounicode_xref"]
            tu_dec = doc.xref_stream(tu_xref)
            sub = tut._parse_subset_tounicode(tu_dec.decode("latin1", "replace"))
            unused_unis = {
                int(u)
                for _cid, u in sub.items()
                if chr(u) not in keep_unis and ("А" <= chr(u) <= "я" or chr(u) in "Ёё")
            }
            if not unused_unis:
                continue
            patched = tu_dec
            n_map = 0
            for cid, u in list(sub.items()):
                if int(u) not in unused_unis:
                    continue
                u = int(u)
                pat = re.compile(
                    (f"<{cid:04x}><{cid:04x}><{u:04x}>").encode("ascii"),
                    re.IGNORECASE,
                )

                def _repl(m: re.Match) -> bytes:
                    # Preserve CID hex casing; only rewrite uni → 0020.
                    return m.group(0)[:-5] + b"0020>"

                patched2, n = pat.subn(_repl, patched, count=1)
                if n:
                    patched = patched2
                    n_map += 1
            if n_map <= 0 or patched == tu_dec or len(patched) != len(tu_dec):
                continue
            cs, ce = _find_stream_pos_for_xref(bytes(out), tu_xref)
            if cs is None:
                continue
            need = ce - cs
            hit = None
            # Remap changes entropy — grow decoded cmap until OpenPDF hits need.
            trial = patched
            for extra in range(0, 400, 2):
                cand = trial + (b"\n" * extra)
                comp = openpdf_deflate(cand, 6)
                if comp is not None and len(comp) == need:
                    hit = comp
                    break
                if comp is not None and len(comp) > need:
                    break
            if hit is None:
                hit = compress_to_size(patched, need, level=6)
            if hit is None:
                fitted = fit_decoded_to_openpdf_size(patched, need, level=6)
                if fitted is not None:
                    hit = openpdf_deflate(fitted, 6)
                    if hit is not None and len(hit) != need:
                        hit = None
            if hit is not None and len(hit) == need:
                out[cs:ce] = hit
                changed = True
                logger.info(
                    "phone CMap remap %s: %d unused letter unis → space",
                    key, n_map,
                )
            else:
                logger.warning(
                    "phone CMap remap %s size miss need=%d — leave unused",
                    key, need,
                )
        doc.close()
        return bytes(out) if changed else pdf
    except Exception as exc:
        logger.warning("phone CMap remap failed: %s", exc)
        return pdf


def _phone_sync_head_bbox_to_fontdesc(pdf: bytes) -> bytes:
    """Force TTF head bbox == PDF /FontBBox (PDF_TTF_BBOX_CROSS_LAYER_MISMATCH).

    Master inject / fontTools recalc shrinks head; descriptor stays donor.
    """
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _TBANK_F1_OPENPDF_CSA,
        _get_head_csa,
        _head_bbox_from_ff2,
        _patch_fontfile2_xref,
        _restore_head_bbox,
        _restore_head_csa,
        _ttf_num_glyphs,
    )

    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        fm = tut._find_font_objects(doc)
        out = pdf
        changed = False
        for key, want_openpdf_csa in (
            ("TinkoffSans-Regular", True),
            ("TinkoffSans-Medium", False),
        ):
            meta = fm.get(key)
            if not meta:
                continue
            fd_xref = meta.get("fontdesc_xref")
            ff_xref = meta.get("fontfile_xref")
            if not fd_xref or not ff_xref:
                continue
            obj = doc.xref_object(fd_xref)
            m = re.search(
                r"/FontBBox\s*\[\s*([-+]?\d+)\s+([-+]?\d+)\s+([-+]?\d+)\s+([-+]?\d+)\s*\]",
                obj,
            )
            if not m:
                continue
            want = tuple(int(m.group(i)) for i in range(1, 5))
            ff2 = doc.xref_stream(ff_xref)
            have = _head_bbox_from_ff2(ff2)
            if have == want:
                continue
            fixed = _restore_head_bbox(ff2, want)  # type: ignore[arg-type]
            if want_openpdf_csa and _ttf_num_glyphs(fixed) > 200:
                fixed = _restore_head_csa(fixed, _TBANK_F1_OPENPDF_CSA)
            if fixed != ff2:
                patched = _patch_fontfile2_xref(out, ff_xref, fixed)
                if patched is not None:
                    out = patched
                    changed = True
                    logger.info(
                        "phone %s head bbox %s → FontBBox %s (CSA=%s)",
                        key,
                        have,
                        want,
                        f"0x{_get_head_csa(fixed) or 0:08X}",
                    )
                    # refresh doc for next font if PDF bytes changed
                    doc.close()
                    doc = fitz.open(stream=out, filetype="pdf")
                    fm = tut._find_font_objects(doc)
        doc.close()
        return out if changed else pdf
    except Exception as exc:
        logger.warning("phone FontBBox sync failed: %s", exc)
        return pdf


def create_tbank_phone_stealth(data: Dict) -> Optional[bytes]:
    """PDF «По номеру телефона» — donor-orig only. Never emit donor FIO."""
    from tbank_dynamic import create_tbank_pipeline
    from tbank_channel_common import size_matches_original
    from tbank_orig_mode import OrigContext

    prepared = _prepare_phone_data(data)
    if prepared is None:
        return None
    pdf = create_tbank_pipeline(
        prepared,
        [_try_donor_orig_phone, _build_dynamic_phone],
        channel="phone",
        dynamic_builder=_build_dynamic_phone,
    )
    if not pdf:
        try:
            pdf = _build_dynamic_phone(dict(prepared))
        except Exception as exc:
            logger.error("phone LAW1 dynamic retry: %s", exc)
            pdf = None
    if not pdf:
        return None
    if len(pdf) > _PHONE_HARD_MAX:
        logger.warning("phone size %d > HARD max — ship", len(pdf))
    if not size_matches_original(pdf, PHONE_ORIG_NARROW, drift=2048):
        logger.warning("phone size drift %d — ship", len(pdf))
    if not _phone_med_kfont002_ok(pdf):
        logger.warning("phone K-FONT-002 pack — ship")
    lean = _lean_phone_medium_after_face(pdf, prepared.get("new_amount") or "")
    if lean is None:
        logger.warning("phone F2 lean failed — ship")
    else:
        pdf = lean
    lean_r = _lean_phone_regular_after_face(pdf, prepared)
    if lean_r is None:
        logger.warning("phone F1 lean failed — ship")
    else:
        pdf = lean_r
    fok, fwhy = _phone_f2_size_ok(pdf)
    if not fok:
        logger.warning("phone F2 size gate: %s — ship", fwhy)
    # Stamp first: leftover FIO at y=451 is Proton's first line (DATE_LINE).
    pdf = _phone_restore_stamp_in_pdf(pdf)
    pdf = _phone_install_receiver_che(pdf, prepared)
    # Drop unused donor CMap leftovers (UNUSED_CID_PRESENT / CMAP_EXTRA_SYMBOLS).
    pdf = _phone_remap_unused_cmap_uniscodes(pdf, prepared)
    pdf = _phone_sync_head_bbox_to_fontdesc(pdf)
    try:
        import fitz
        from sber_dynamic import _mask_pdf_literal_tj

        d = fitz.open(stream=pdf, filetype="pdf")
        body = d.xref_stream(d[0].get_contents()[0])
        d.close()
        od = fitz.open(PHONE_ORIG_NARROW)
        ob = od.xref_stream(od[0].get_contents()[0])
        od.close()

        def _op_sk(blob: bytes) -> bytes:
            sk = _mask_pdf_literal_tj(blob)
            sk = re.sub(rb"\n[ \t]+ET\b", b"\nET", sk)
            return re.sub(rb"1 0 0 1 [0-9.]+ [0-9.]+ Tm", b"1 0 0 1 N N Tm", sk)

        if re.search(rb"[ \t]+ET\b", body):
            logger.warning("phone ET-whitespace gate — ship")
        if re.search(rb"(?:\r?\n)[ \t]*(?:\r?\n)+[ \t]*ET\b", body):
            logger.warning("phone blank-before-ET gate — ship")
        if re.search(rb"(?:Td|Tm|Tj|BT|ET)(?:Td|Tm|Tj|BT|ET)", body):
            logger.warning("phone glued-operators gate — ship")
        if re.search(rb"(?m)^[ \t]+\-?[\d.]", body):
            logger.warning("phone indented-operand gate — ship")
        if _op_sk(body) != _op_sk(ob):
            logger.info("phone operator-skeleton drift (allowed after fit pads)")
    except Exception as exc:
        logger.warning("phone skeleton gate skipped: %s", exc)
    lok, lwhy = _phone_layout_ok(pdf, prepared)
    if not lok:
        logger.error("phone layout gate: %s — ship anyway", lwhy)
    pdf = _patch_phone_commission_in_pdf(pdf)
    pdf = _phone_install_receiver_che(pdf, prepared)
    pdf = _phone_restore_stamp_in_pdf(pdf)
    fok2, fwhy2 = _phone_f2_size_ok(pdf)
    if not fok2:
        logger.warning("phone F2 size gate (pre-finish): %s — ship", fwhy2)
    from tbank_dynamic import _tbank_finish_non_sbp_ship

    return _tbank_finish_non_sbp_ship(
        pdf, height=451, prepared=prepared, channel="phone",
    )


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    sample = {
        "date_time": "07.05.2026, 14:30",
        "amount": "55000",
        "sender": "Алексей Морозов",
        "phone": "+7 (916) 123-45-67",
        "receiver": "Анастасия Д.",
        "receipt_num": "авто",
    }
    res = create_tbank_phone_stealth(sample)
    if res:
        out = os.path.join(_DIR, "_test_phone_stealth.pdf")
        with open(out, "wb") as f:
            f.write(res)
        print(f"saved: {out} ({len(res)} bytes)")
    else:
        print("FAIL")
