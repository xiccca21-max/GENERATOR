"""
ALFA PHONE — перевод клиенту Альфа-Банка по номеру телефона
(donor-orig как SBP/CARD, шаблон iOS Quartz /G1).
"""
import os
import re
import logging
import random
import shutil
from datetime import datetime, timedelta
from typing import Dict, Optional

from alfa_phone_orig_mode import AlfaPhoneOrigContext
from time_msk import now_msk

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
PHONE_ORIG = os.path.join(_DIR, "templates", "Alfa_phone_original.pdf")
PHONE_UNLOCKED = os.path.join(_DIR, "templates", "Alfa_phone_unlocked.pdf")
_CORPUS_SEED = os.path.join(
    os.path.expanduser("~"),
    "OneDrive",
    "Desktop",
    "чеки",
    "альфа чеки",
    "по номеру телефона на альфа банк.pdf",
)
_CORPUS_ALT = os.path.join(
    os.path.expanduser("~"),
    "OneDrive",
    "Desktop",
    "чеки",
    "альфа",
    "Квитанция (13).pdf",
)

_NBSP = "\u00a0"
_ALFA_PHONE_MESSAGE = f"Перевод{_NBSP}денежных{_NBSP}средств"

# (y, x) — value slots из Quartz donor
PHONE_COORDS = {
    "date_formed": (779.15, 452.788),
    "amount": (664.288, 35.45),
    "commission": (621.394, 35.45),
    "date_time": (578.5, 35.45),
    "operation_num": (535.606, 35.45),
    "receiver": (664.288, 304.75),
    "phone": (621.394, 304.75),
    "account": (578.5, 304.75),
    "message": (535.606, 304.75),
}


def _ensure_template() -> str:
    os.makedirs(os.path.dirname(PHONE_ORIG), exist_ok=True)
    src = None
    for cand in (_CORPUS_SEED, _CORPUS_ALT):
        if cand and os.path.isfile(cand):
            src = cand
            break
    from alfa_corpus import rank_donors

    ranked = rank_donors("phone")
    if ranked:
        src = ranked[0]
    if os.path.isfile(PHONE_ORIG):
        ctx = AlfaPhoneOrigContext()
        if ctx.load(PHONE_ORIG) and ctx.slot_size_at(*PHONE_COORDS["phone"]) >= 8:
            return PHONE_ORIG
    if src and os.path.isfile(src):
        shutil.copy2(src, PHONE_ORIG)
        # also drop a copy into corpus dir for classify
        corpus_dir = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "альфа")
        if os.path.isdir(corpus_dir):
            dst = os.path.join(corpus_dir, "альфа телефон.pdf")
            if not os.path.isfile(dst):
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    pass
    return PHONE_ORIG if os.path.isfile(PHONE_ORIG) else (src or "")


def _iter_donors(data: Dict) -> list:
    from alfa_corpus import canonical_paths, corpus_paths

    paths = canonical_paths("phone") or corpus_paths("phone")
    scored = []
    for p in paths:
        ctx = AlfaPhoneOrigContext()
        if not ctx.load(p):
            continue
        prepared = _prepare_phone(data, available=ctx.available_chars)
        ok_g, _ = ctx.can_render("".join(prepared.values()))
        ph_slot = ctx.slot_size_at(*PHONE_COORDS["phone"])
        recv_slot = ctx.slot_size_at(*PHONE_COORDS["receiver"])
        scored.append((1 if ok_g else 0, ph_slot, recv_slot, p))
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    out = [p for _, _, _, p in scored]
    # Prefer lean Quartz (~69.7–71.6KB, HARD ≤73000). Unlocked ~75.5KB →
    # ALFA_FILE_SIZE_STRONG_OUTLIER.
    lean, fat = [], []
    for p in out:
        try:
            sz = os.path.getsize(p)
        except OSError:
            lean.append(p)
            continue
        (lean if 67_500 <= sz <= 73_000 else fat).append(p)
    prefs = [p for p in (_ensure_template(),) if p and os.path.isfile(p)]
    for pref in reversed(prefs):
        if pref in lean:
            lean.remove(pref)
        if pref in fat:
            fat.remove(pref)
        try:
            sz = os.path.getsize(pref)
        except OSError:
            sz = 0
        if 67_500 <= sz <= 73_000:
            lean.insert(0, pref)
        else:
            fat.append(pref)
    unlocked = [p for p in (PHONE_UNLOCKED,) if p and os.path.isfile(p)]
    for u in unlocked:
        if u in lean:
            lean.remove(u)
        if u in fat:
            fat.remove(u)
    for seed in (_CORPUS_SEED, _CORPUS_ALT):
        if seed and os.path.isfile(seed) and seed not in lean and seed not in fat and seed not in unlocked:
            try:
                sz = os.path.getsize(seed)
            except OSError:
                sz = 0
            (lean if 67_500 <= sz <= 73_000 else fat).append(seed)
    return lean + fat + unlocked


def _normalize_field(s: str) -> str:
    return (s or "").replace(_NBSP, " ").strip()


def _verify_committed(pdf: bytes, prepared: Dict[str, str]) -> bool:
    ctx = AlfaPhoneOrigContext()
    if not ctx.load_bytes(pdf):
        logger.warning("Alfa PHONE verify: load_bytes failed")
        return False
    for key in PHONE_COORDS:
        if key not in prepared:
            continue
        got = _normalize_field(ctx.extract_at(*PHONE_COORDS[key]))
        want = _normalize_field(prepared[key])
        if key == "account" and (not got or not want or "*" not in got):
            logger.warning(
                "Alfa PHONE verify empty/bad account: got=%r want=%r",
                got,
                want,
            )
            return False
        if got != want:
            logger.warning(
                "Alfa PHONE verify mismatch %s: got=%r want=%r",
                key,
                got,
                want,
            )
            return False
    return True


_PHONE_IDENTITY_KEYS = frozenset({
    "receiver", "phone", "account", "message", "recipient_bank",
    # amount/commission: никогда не резать «RUR» → иначе «654600 R».
    "amount", "commission",
})


def _attempt(path: str, data: Dict, *, tag: str, max_trials: int = 6) -> Optional[bytes]:
    """Exact flate + Quartz entropy-nudge; never Length/xref rebuild."""
    from alfa_phone_orig_mode import _oracle_near_flate

    base = dict(data)
    from alfa_sbp_stealth import _is_auto_token

    auto_operation = _is_auto_token(base.get("operation_num") or base.get("operation_number"))
    # Канонический текст пользователя — без available-подмены.
    identity_seed = _prepare_phone(base, available=None)
    identity_want = {k: identity_seed[k] for k in _PHONE_IDENTITY_KEYS if k in identity_seed}

    for trial in range(max(1, max_trials)):
        ctx = AlfaPhoneOrigContext()
        if not ctx.load(path):
            return None
        prepared = _prepare_phone(
            base if trial == 0 or not auto_operation else {**base, "operation_num": "авто"},
            available=None,
        )
        prepared.update(identity_want)

        slot_failed = False
        for key, (y, x) in PHONE_COORDS.items():
            if key not in prepared:
                continue
            need = len(prepared[key].rstrip(_NBSP))
            if "RUR" in prepared[key].replace(_NBSP, " "):
                need += 1
            have = ctx.slot_size_at(y, x)
            if need <= have:
                continue
            if have <= 0 or not ctx.grow_slot_at(y, x, need):
                logger.info(
                    "[%s %s] slot grow failed %s need=%d have=%d",
                    tag, os.path.basename(path), key, need, have,
                )
                slot_failed = True
                break
        if slot_failed:
            continue

        amount_ok = True
        for key in ("amount", "commission"):
            if key in prepared and "RUR" not in prepared[key].replace(_NBSP, " "):
                logger.info("[%s %s] amount lost RUR", tag, os.path.basename(path))
                amount_ok = False
                break
        if not amount_ok:
            continue

        ok, why = ctx.fits_fields(PHONE_COORDS, prepared)
        if not ok:
            logger.info("[%s %s] skip fit: %s", tag, os.path.basename(path), why)
            continue
        for key, (y, x) in PHONE_COORDS.items():
            if key in prepared and not ctx.replace_at(y, x, prepared[key]):
                logger.info("[%s %s] replace fail: %s", tag, os.path.basename(path), key)
                # не return None — следующий trial
                ok = False
                break
        if not ok:
            continue

        stream_b = bytes(ctx.stream)
        # Quartz genuines: max decoded /Contents ≥5012 (HARD <4912).
        # SEQ phone shells sit ~4780 — pad before ET, then re-fit flate.
        _QUARTZ_CONTENT_DEC_MIN = 5012
        if len(stream_b) < _QUARTZ_CONTENT_DEC_MIN:
            import re as _re

            need = _QUARTZ_CONTENT_DEC_MIN - len(stream_b)
            ets = [
                m.start()
                for m in _re.finditer(
                    rb"(?<![A-Za-z0-9])ET(?![A-Za-z0-9])", stream_b,
                )
            ]
            pt = ets[-1] if ets else len(stream_b)
            stream_b = stream_b[:pt] + (b" " * need) + stream_b[pt:]
            ctx.stream = bytearray(stream_b)
        comp = _oracle_near_flate(stream_b, ctx.zlib_level)
        if not comp or len(comp) != ctx.orig_comp_len:
            from alfa_phone_orig_mode import _nudge_quartz_exact_flate

            nudged = _nudge_quartz_exact_flate(
                stream_b, ctx.orig_comp_len, ctx.zlib_level,
            )
            if nudged is None:
                logger.info(
                    "[%s %s] flate %s≠%d trial=%d — reject",
                    tag,
                    os.path.basename(path),
                    None if not comp else len(comp),
                    ctx.orig_comp_len,
                    trial,
                )
                continue
            ctx.stream = bytearray(nudged)
            comp = _oracle_near_flate(bytes(ctx.stream), ctx.zlib_level)
            if not comp or len(comp) != ctx.orig_comp_len:
                continue

        result = ctx.commit()
        if result is None:
            continue
        # Proton: donor /ID + mutated content → ALFA_TRAILER_ID_REUSED.
        result = _randomize_trailer_id(result)
        if not _verify_committed(result, prepared):
            logger.info("[%s %s] post-commit verify failed", tag, os.path.basename(path))
            continue
        from alfa_sbp_stealth import _fit_alfa_fontfile_size

        # Quartz corpus: 69706–71632 (Proton HARD ~67500–73000). Keep native
        # image Flate — never Oracle-compact swap (OnlyPDF virtual printer).
        sized = _fit_alfa_fontfile_size(
            result,
            bytes(ctx.stream),
            lean=True,
            compact_images=False,
            size_min=69_706,
            size_max=71_632,
            target=70_500,
        )
        if sized is None or not _verify_committed(sized, prepared):
            logger.info("[%s %s] FontFile2 size fit failed", tag, os.path.basename(path))
            continue
        result = sized
        logger.info("🔴 ALFA PHONE %s: %d bytes (trial %d)", tag, len(result), trial)
        return result

    logger.warning("[%s %s] commit failed after retries", tag, os.path.basename(path))
    return None


def _fmt_amount(amount_raw: str, *, max_chars: int = 0) -> str:
    """Всегда с разрядами: «300 RUR », «10 000 RUR ».

    Never flood trailing NBSP after RUR (ALFA_AMOUNT_TYPOGRAPHY_ANOMALY).
    """
    digits = re.sub(r"\D", "", amount_raw or "0")
    n = int(digits) if digits else 0
    grouped = f"{n:,}".replace(",", _NBSP)
    body = f"{grouped}{_NBSP}RUR{_NBSP}"
    if max_chars > 0 and len(body) > max_chars:
        bare = f"{grouped}{_NBSP}RUR"
        if len(bare) <= max_chars:
            return bare + (_NBSP if len(bare) < max_chars else "")
        return bare[:max_chars]
    return body


def _fmt_commission(*, max_chars: int = 6) -> str:
    """Как в оригинале: «0 RUR » (len=6) — one trailing NBSP only."""
    body = f"0{_NBSP}RUR{_NBSP}"
    return body[:max_chars] if max_chars > 0 else body


def _fmt_phone_masked(phone: str) -> str:
    """Как в оригинале: 913***4511 (10 цифр без +7)."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("8") and len(d) == 11:
        d = "7" + d[1:]
    if len(d) == 11 and d.startswith("7"):
        d = d[1:]
    if len(d) == 10:
        return f"{d[:3]}***{d[-4:]}"
    if "***" in (phone or ""):
        return re.sub(r"\s+", "", phone)
    return phone.strip()


def _fmt_account_masked(account: str) -> str:
    d = re.sub(r"\D", "", account or "")
    if len(d) >= 20:
        return f"{d[:6]}**********{d[-4:]}"
    if "*" in (account or ""):
        return re.sub(r"\s+", "", account)
    return account.strip()


def _gen_debit_account() -> str:
    """Счёт списания 20 цифр (40817…) — маска 408178**********XXXX."""
    import secrets

    tail = f"{secrets.randbelow(10_000):04d}"
    mid = f"{secrets.randbelow(10**10):010d}"
    return f"408178{mid}{tail}"


def _resolve_account(raw: str) -> str:
    s = str(raw or "").strip()
    if not s or s.lower() in ("авто", "auto", "-"):
        return _fmt_account_masked(_gen_debit_account())
    return _fmt_account_masked(s)


def _parse_dt(date_in: str) -> datetime:
    from alfa_sbp_stealth import _parse_dt as _sbp_parse

    return _sbp_parse(date_in)


def _fmt_datetime(date_in, *, with_seconds: bool = True) -> str:
    from alfa_sbp_stealth import _fmt_datetime as _sbp_fmt

    return _sbp_fmt(date_in, with_seconds=with_seconds)


def _gen_phone_op_num(dt: datetime) -> str:
    """PHONE: C07 + DDMMYY + 7 цифр (корпус / PROTON ^C07\\d{13}$).

    НЕ вшивать HHMMSS — как SBP C16.
    """
    import secrets

    date_part = dt.strftime("%d%m%y")
    forbidden = dt.strftime("%H%M%S")
    for _ in range(32):
        tail7 = f"{secrets.randbelow(10_000_000):07d}"
        if tail7[:6] != forbidden:
            return f"C07{date_part}{tail7}"
    return f"C07{date_part}{(int(forbidden) + 17) % 1_000_000:06d}{secrets.randbelow(10)}"


def _randomize_trailer_id(pdf: bytes) -> bytes:
    import random

    rid = f"{random.getrandbits(128):032x}".encode("ascii")
    return re.sub(
        rb"/ID\s*\[\s*<[0-9A-Fa-f]{32}>\s*<[0-9A-Fa-f]{32}>\s*\]",
        b"/ID [<" + rid + b"><" + rid + b">]",
        pdf,
        count=1,
    )


def _fmt_receiver_masked(receiver: str, available: Optional[set] = None) -> str:
    """Alfa phone face: «Вол**в Д. В.» — never emit full unmasked FIO.

    Proton ALFA_PHONE_RECIPIENT_INITIALS: masked surname must carry **two**
    initials (corpus «Д. В.»). A single trailing «О.» is SEQ-template HARD.
    """
    raw = (receiver or "").replace("\xa0", " ").strip()
    if not raw:
        return raw
    parts = [p for p in raw.replace(".", " ").split() if p]
    if not parts:
        return raw.replace(" ", _NBSP)
    sur = parts[0]
    if len(sur) >= 5:
        sur_m = sur[:3] + "**" + sur[-1]
    elif len(sur) == 4:
        sur_m = sur[:2] + "**" + sur[-1]
    elif len(sur) == 3:
        sur_m = sur[:2] + "**" + sur[-1]
    else:
        sur_m = sur[0] + "**"
    inits: list[str] = []
    for p in parts[1:]:
        ch = p.strip().rstrip(".")
        if ch:
            inits.append(ch[0].upper() + ".")
    # Always exactly two initials on masked phone face.
    pool = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯ"
    if available:
        pool = "".join(ch for ch in pool if ch in available) or pool
    while len(inits) < 2:
        pick = None
        for ch in pool:
            cand = ch + "."
            if cand not in inits:
                pick = cand
                break
        inits.append(pick or ("В." if "А." in inits else "А."))
    inits = inits[:2]
    out = sur_m + " " + " ".join(inits)
    return out.replace(" ", _NBSP)


def _prepare_phone(
    data: Dict, *, available: Optional[set] = None
) -> Dict[str, str]:
    date_in = str(data.get("date_time") or data.get("date") or "сейчас")
    op_dt = _parse_dt(date_in)
    op = str(data.get("operation_num") or data.get("operation_number") or "")
    if not op or op.lower() in ("авто", "auto", "-"):
        op = _gen_phone_op_num(op_dt)
    msg = str(data.get("message") or "").strip()
    if not msg or msg.lower() in ("авто", "auto", "-"):
        msg = _ALFA_PHONE_MESSAGE
    else:
        msg = msg.replace(" ", _NBSP)
    # One MSK clock for date_formed + date_time (no random minute skew).
    return {
        "date_formed": _fmt_datetime(op_dt, with_seconds=False),
        "amount": _fmt_amount(str(data.get("amount", "0"))),
        "commission": _fmt_commission(),
        "date_time": _fmt_datetime(op_dt, with_seconds=True),
        "operation_num": op + _NBSP,
        "receiver": _fmt_receiver_masked(str(data.get("receiver", "")), available),
        "phone": _fmt_phone_masked(str(data.get("phone", ""))),
        "account": _resolve_account(str(data.get("account", ""))),
        "message": msg,
    }


def _append_phone_cid_width(pdf: bytearray, cid_xref: int, cid: int, width: int) -> bool:
    """Дописать `cid [ width ]` в Quartz /W (часто indirect obj), иначе /DW 1000 → дыры."""
    from tbank_orig_mode import find_object_range
    from tbank_sbp_stealth import _replace_byte_range_and_rebuild

    obj_rng = find_object_range(bytes(pdf), cid_xref)
    if not obj_rng:
        return False
    o_start, o_end = obj_rng
    raw_obj = bytes(pdf[o_start:o_end]).decode("latin1", "replace")
    m_w = re.search(r"/W\s+(\d+)\s+0\s+R", raw_obj)
    if m_w:
        w_xref = int(m_w.group(1))
        wr = find_object_range(bytes(pdf), w_xref)
        if not wr:
            return False
        ws, we = wr
        wraw = bytes(pdf[ws:we]).decode("latin1", "replace")
        if re.search(rf"(?:^|[\s\[]){cid}\s*\[", wraw):
            return True
        # объект вида: N 0 obj\n[ 1 [ ... ] ]\nendobj — вставка перед последней ]
        endobj = wraw.rfind("endobj")
        if endobj < 0:
            return False
        body = wraw[:endobj]
        close = body.rfind("]")
        if close < 0:
            return False
        entry = f" {int(cid)} [ {int(width)} ]"
        if "\n" in body[body.find("[") : close]:
            entry = entry + "\n"
        new_w = body[:close] + entry + body[close:] + wraw[endobj:]
        new_b = new_w.encode("latin1")
        if len(new_b) == we - ws:
            pdf[ws:we] = new_b
            return True
        patched = _replace_byte_range_and_rebuild(bytes(pdf), ws, we, new_b)
        if patched is None:
            return False
        pdf[:] = patched
        return True

    import alfa_font_extend as afe

    return afe._append_oracle_cid_width(pdf, cid_xref, cid, width)


def _append_phone_bfrange(tu_bytes: bytes, cid: int, cp: int) -> Optional[bytes]:
    """Добавить CID→unicode в Quartz ToUnicode (beginbfrange)."""
    text = tu_bytes.decode("latin1", "replace")
    if re.search(rf"<{cid:04X}>\s*<{cid:04X}>\s*<{cp:04X}>", text, re.I):
        return tu_bytes
    nl = "\r\n" if "\r\n" in text else "\n"
    block = (
        f"1 beginbfrange{nl}"
        f"<{cid:04X}><{cid:04X}><{cp:04X}>{nl}"
        f"endbfrange{nl}"
    )
    insert_at = text.find("endcmap")
    if insert_at < 0:
        return None
    return (text[:insert_at] + block + text[insert_at:]).encode("latin1")


def _phone_glyphs_ok(ctx: AlfaPhoneOrigContext, text: str) -> tuple[bool, list[str]]:
    """Require a CID mapping and a non-empty contour for every visible glyph."""
    import alfa_font_extend as afe

    try:
        ff2 = afe._ff2_read_decompressed(ctx.pdf_bytes, ctx.ff2_xref)
    except Exception:
        return False, list(text)
    missing = []
    for ch in text:
        if ch in ("\n", "\r", "\t", " ", _NBSP):
            continue
        cid = ctx.uni_to_cid.get(ord(ch))
        if cid is None or not afe._glyph_slot_has_ink(ff2, cid):
            missing.append(ch)
    return not missing, missing


def _ensure_phone_font_chars(base_path: str, text: str) -> Optional[str]:
    """Докинуть недостающие глифы в Quartz /G1 (как LIB у SBP)."""
    import tempfile
    from io import BytesIO
    from fontTools.ttLib import TTFont
    import alfa_font_extend as afe
    import alfa_glyph_library as agl
    from alfa_corpus import canonical_paths

    ctx = AlfaPhoneOrigContext()
    if not ctx.load(base_path):
        return None
    _, miss = _phone_glyphs_ok(ctx, text)
    seen = set()
    need = []
    for c in miss:
        if c not in seen:
            seen.add(c)
            need.append(c)
    if not need:
        return base_path

    agl.ensure_library()
    sbp_donors = canonical_paths("sbp") or []
    working = base_path

    for ch in need:
        cp = afe._char_codepoint(ch)
        ctx = AlfaPhoneOrigContext()
        if not ctx.load(working):
            return None
        if ch in ctx.available_chars:
            ff2 = afe._ff2_read_decompressed(ctx.pdf_bytes, ctx.ff2_xref)
            cid0 = ctx.uni_to_cid.get(cp)
            if cid0 is not None and afe._glyph_slot_has_ink(ff2, cid0):
                continue

        with open(working, "rb") as f:
            pdf = bytearray(f.read())
        ff2_ttf = afe._ff2_read_decompressed(bytes(pdf), ctx.ff2_xref)
        dst_ft = TTFont(BytesIO(ff2_ttf))
        upem = int(dst_ft["head"].unitsPerEm) or 2048
        n_glyphs = len(dst_ft.getGlyphOrder())
        del dst_ft

        simple = None
        aw_font = upem // 2
        lsb_font = 0
        pdf_w = 500
        src_label = "library"

        from alfa_orig_mode import AlfaOrigContext

        for donor in sbp_donors:
            ctx_s = AlfaOrigContext()
            refs_s = afe._load_font_xrefs(donor)
            if not (ctx_s.load(donor) and refs_s):
                continue
            src_cid = ctx_s.uni_to_cid.get(cp)
            if src_cid is None:
                continue
            with open(donor, "rb") as donor_file:
                donor_pdf = donor_file.read()
            src_ff2 = afe._ff2_read_decompressed(donor_pdf, refs_s["ff2"])
            if not afe._glyph_slot_has_ink(src_ff2, src_cid):
                continue
            src_ft = TTFont(BytesIO(src_ff2))
            src_name = src_ft.getGlyphOrder()[src_cid]
            simple = afe._as_simple_glyph(src_ft, src_name)
            if simple is None:
                continue
            aw_font, lsb_font = src_ft["hmtx"].metrics[src_name]
            aw_font = int(aw_font)
            lsb_font = int(lsb_font)
            src_upem = int(src_ft["head"].unitsPerEm) or upem
            if src_upem != upem and src_upem > 0:
                from alfa_glyph_library import _scale_glyph

                scale = upem / float(src_upem)
                simple = _scale_glyph(simple, scale)
                aw_font = int(round(aw_font * scale))
                lsb_font = int(round(lsb_font * scale))
            pdf_w = int(
                ctx_s.widths.get(src_cid) or afe._pdf_width_from_font_aw(aw_font, upem)
            )
            src_label = os.path.basename(donor)
            break

        if simple is None:
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
                pdf_w = afe._pdf_width_from_font_aw(aw_font, upem)
                src_label = "library"

        if simple is None or getattr(simple, "numberOfContours", 0) <= 0:
            logger.warning("Alfa PHONE: no glyph for %r", ch)
            return None

        dst_cid = n_glyphs if n_glyphs < 255 else None
        if dst_cid is None:
            logger.warning("Alfa PHONE: no free CID for %r", ch)
            return None

        new_ff2 = afe._install_simple_glyph(
            ff2_ttf, dst_cid, simple, int(aw_font), int(lsb_font),
        )
        if not new_ff2:
            return None
        if not afe._patch_ff2_decompressed(pdf, ctx.ff2_xref, new_ff2):
            return None

        # xrefs may be stable; reload for safety
        ctx2 = AlfaPhoneOrigContext()
        if not ctx2.load_bytes(bytes(pdf)):
            return None
        tu_dec = afe._tu_read_decompressed(bytes(pdf), ctx2.tu_xref)
        tu_new = _append_phone_bfrange(tu_dec, dst_cid, cp)
        if tu_new is None or not afe._patch_tu_decompressed(pdf, ctx2.tu_xref, tu_new):
            return None
        ctx3 = AlfaPhoneOrigContext()
        if not ctx3.load_bytes(bytes(pdf)):
            return None
        _append_phone_cid_width(pdf, ctx3.cid_xref, dst_cid, int(pdf_w))

        fd, out = tempfile.mkstemp(suffix=".pdf", prefix="alfa_phone_inj_")
        os.close(fd)
        with open(out, "wb") as f:
            f.write(bytes(pdf))
        logger.info(
            "Alfa PHONE glyph inject %r cid=%d from %s",
            ch,
            dst_cid,
            src_label,
        )
        working = out

    final_ctx = AlfaPhoneOrigContext()
    if not final_ctx.load(working):
        return None
    ok, missing = _phone_glyphs_ok(final_ctx, text)
    if not ok:
        logger.warning("Alfa PHONE font extend incomplete: %s", "".join(missing[:12]))
        return None
    return working


def create_alfa_phone_stealth(data: Dict) -> Optional[bytes]:
    from alfa_sbp_stealth import _blocked_alfa_chars

    # NATIVE only: LIB FontFile2 growth → OnlyPDF FAKE (need in-place glyf later).
    pool = list(_iter_donors(data))[:16]
    if not pool:
        logger.error("Alfa PHONE: нет доноров")
        return None

    for outer in range(3):
        work_data = dict(data)
        from alfa_sbp_stealth import _is_auto_token

        auto_operation = _is_auto_token(
            data.get("operation_num") or data.get("operation_number")
        )
        if auto_operation:
            work_data["operation_num"] = "авто"
        if "receipt_num" in work_data:
            work_data["receipt_num"] = "авто"

        for path in pool:
            ctx = AlfaPhoneOrigContext()
            if not ctx.load(path):
                continue
            # Keep user date («сейчас»/«авто»/explicit). Only fall back to donor
            # calendar when the user left the field empty (should not happen).
            user_dt = str(work_data.get("date_time") or work_data.get("date") or "").strip()
            from alfa_sbp_stealth import _is_auto_datetime

            if not user_dt or _is_auto_datetime(user_dt):
                # Resolve to now once so op#/message stay consistent this attempt.
                work_data["date_time"] = "сейчас"
            if auto_operation:
                work_data["operation_num"] = "авто"
            prepared = _prepare_phone(work_data, available=None)
            all_text = "".join(prepared.values())
            ok, _ = _phone_glyphs_ok(ctx, all_text)
            work_path = path
            if not ok:
                injected = _ensure_phone_font_chars(path, all_text)
                if not injected:
                    continue
                work_path = injected
                ctx2 = AlfaPhoneOrigContext()
                if not ctx2.load(work_path) or not _phone_glyphs_ok(ctx2, all_text)[0]:
                    continue
            hit = _attempt(
                work_path,
                work_data,
                tag="NATIVE",
                max_trials=12,
            )
            if hit:
                return hit

    logger.error("Alfa PHONE: все пути не удались (charset/slots)")
    return None


def check_text(text: str) -> list:
    import alfa_glyph_library as agl

    agl.ensure_library()
    chars = set(agl.available_chars())
    from alfa_sbp_stealth import _BLOCKED_ALFA_CHARS

    return [
        c for c in text
        if c in _BLOCKED_ALFA_CHARS or (c not in chars and c not in " \t\n")
    ]
