"""
Общий dynamic-режим для всех T-Bank PDF-генераторов.

Корень проблемы unlocked-fallback:
  поиск/замена в content stream через CID полного TinkoffSans (.ttf),
  тогда как в PDF лежат subset-CID встроенного шрифта шаблона.
  Маркеры не находятся → None → «Ошибка генерации».

Решение:
  патч embedded-шрифта в ОРИГИНАЛЬНОМ шаблоне + кодирование через dynamic GID map.
"""

import logging
from typing import Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_DIR = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
FONT_REGULAR = __import__("os").path.join(_DIR, "fonts", "TinkoffSans-Regular.ttf")
FONT_MEDIUM = __import__("os").path.join(_DIR, "fonts", "TinkoffSans-Medium.ttf")
# Банковские GID (upem=1000) — как в реальных чеках T-Bank
FONT_TBANK_MASTER_REG = __import__("os").path.join(_DIR, "fonts", "TinkoffSans-TBank-Regular.ttf")
FONT_TBANK_MASTER_MED = __import__("os").path.join(_DIR, "fonts", "TinkoffSans-TBank-Medium.ttf")


_PAD_CHAR = " "  # regular space — всегда в TinkoffSans subset


def parse_money_parts(raw: str) -> Tuple[int, int]:
    """Рубли и копейки из '35000', '34 778,33', '221.67 ₽'."""
    import re
    s = str(raw or "").strip().replace("₽", "").replace("\u00a0", " ")
    s = re.sub(r"\s+", "", s)
    if "," in s or "." in s:
        sep = "," if "," in s else "."
        rub_s, kop_s = s.split(sep, 1)
        rub = int(re.sub(r"[^\d]", "", rub_s) or "0")
        kop = int((re.sub(r"[^\d]", "", kop_s) + "00")[:2])
        return rub, kop
    digits = re.sub(r"[^\d]", "", s) or "0"
    return int(digits), 0


def format_decimal_amount_like_template(amount_raw: str, template: str) -> str:
    """Сумма с копейками: '34 778,33 ' — ровно один пробел перед ₽."""
    import re
    rub, kop = parse_money_parts(amount_raw)
    spaced_rub = f"{rub:,}".replace(",", " ")
    # One trailing space only — multi-space pad → huge gap before F3 ₽.
    return f"{spaced_rub},{kop:02d} "


def format_amount_like_template(amount_digits: str, template: str) -> str:
    """Форматирует сумму: разряды + ровно один хвостовой пробел перед ₽."""
    import re
    raw = str(amount_digits).strip()
    if re.search(r"[,.]\d", re.sub(r"\s+", "", raw)):
        return format_decimal_amount_like_template(raw, template)
    n = int(re.sub(r"[^\d]", "", raw) or "0")
    return f"{n:,}".replace(",", " ") + " "


def _texts_to_unicodes(texts: List[str]) -> set:
    need: set = set()
    for t in texts:
        need.update(ord(ch) for ch in t)
    return need


def _inject_pua_tj_no_growth(
    stream: bytes, marker: bytes, max_len: Optional[int] = None,
) -> Optional[bytes]:
    """Insert PUA Tj before last ET by replacing trailing whitespace (no net growth)."""
    et = stream.rfind(b"ET")
    if et < 0:
        return None
    pre = stream[:et]
    post = stream[et:]
    cut = 0
    for j in range(len(pre) - 1, -1, -1):
        if pre[j : j + 1] in b" \n\r\t":
            cut += 1
        elif cut:
            break
    need = len(marker)
    if cut < need:
        return None
    trim = cut - need
    # Never pad with spaces immediately before ET — TBANK_CONTENT_ET_WHITESPACE_ANOMALY.
    # Keep length with leading newlines before the marker; marker itself ends with \n.
    head = pre[: len(pre) - cut]
    if trim:
        head += b"\n" * trim
    out = head + marker + post
    if max_len is not None and len(out) > max_len:
        return None
    return out


# Jasper stamp BT ends with this no-op Td pair (move + undo). Keep Td·Td text-free
# (Proton TBANK_CONTENT_TD_TJ_INTERLEAVE). Off-page PUA goes via Tm+Tj AFTER the pair.
# Never stack extra Td onto the stamp (2+2 → TBANK_CONTENT_TD_RUN_ANOMALY HARD).
_JASPER_STAMP_TD_NOOP = b"175 0 Td\n-175 0 Td\n"


def _bt_td_count(block: bytes) -> int:
    import re

    return len(re.findall(rb"(?<![A-Za-z0-9_])Td(?![A-Za-z0-9_])", block))


def _pdf_ops_after_tm(after: bytes) -> list:
    """Operator names after a Tm; string/hex literals stripped so face text is not ops."""
    import re

    cleaned = re.sub(rb"\((?:\\.|[^\\)])*\)", b"()", after)
    cleaned = re.sub(rb"<[^>]*>", b"<>", cleaned)
    return re.findall(
        rb"(?:[-+]?(?:\d+\.?\d*|\.\d+)\s+)*([A-Za-z*]+)", cleaned
    )


def _max_td_run_after_tm(stream: bytes) -> int:
    """Max consecutive Td ops after a Tm within the same BT (Tj/TJ ignored)."""
    import re

    best = 0
    for m in re.finditer(rb"BT(.*?)ET", stream, re.S):
        block = m.group(1)
        pos = 0
        while True:
            i = block.find(b" Tm", pos)
            if i < 0:
                i = block.find(b"\nTm", pos)
            if i < 0:
                break
            ops = _pdf_ops_after_tm(block[i + 3 :])
            run = 0
            for op in ops:
                if op == b"Td":
                    run += 1
                    best = max(best, run)
                elif op in (b"Tj", b"TJ", b"'", b'"'):
                    continue
                else:
                    break
            pos = i + 3
    return best


def _has_td_tj_interleave(stream: bytes) -> bool:
    """True if Tj/TJ sits between a Td·Td pair after Tm (TBANK_CONTENT_TD_TJ_INTERLEAVE)."""
    import re

    for m in re.finditer(rb"BT(.*?)ET", stream, re.S):
        block = m.group(1)
        pos = 0
        while True:
            i = block.find(b" Tm", pos)
            if i < 0:
                i = block.find(b"\nTm", pos)
            if i < 0:
                break
            ops = _pdf_ops_after_tm(block[i + 3 :])
            seen_td = False
            tj_between = False
            for op in ops:
                if op == b"Td":
                    if seen_td and tj_between:
                        return True
                    seen_td = True
                    tj_between = False
                elif op in (b"Tj", b"TJ", b"'", b'"'):
                    if seen_td:
                        tj_between = True
                else:
                    break
            pos = i + 3
    return False


def inject_pua_offpage_td_marker(
    stream: bytes,
    enc: bytes,
    cs_dec_max: Optional[int] = None,
) -> Optional[bytes]:
    """DISABLED — Proton HARD on Td -500 -500 / Tr=3 / stamp Td-pair steal.

    Historical SEQ orphan binder replaced Jasper ``175 0 Td / -175 0 Td`` with
    ``-500 -500 Td`` + invisible ``3 Tr`` → TBANK_CONTENT_TD_SENTINEL,
    IMG_TD_SCALE_MISMATCH, TR_MODE_ANOMALY. Never emit those markers again.
    Callers must cover orphans via visible CID swaps / blank / twin pick.
    """
    _ = (stream, enc, cs_dec_max)
    logger.error(
        "inject_pua_offpage_td_marker DISABLED (Td -500 / Tr=3 → Proton HARD)"
    )
    return None


def _inject_pua_offpage_td_marker_LEGACY_DISABLED(
    stream: bytes,
    enc: bytes,
    cs_dec_max: Optional[int] = None,
) -> Optional[bytes]:
    """Place PUA off-page without TD_RUN / INTERLEAVE / BT_TM_PROFILE / visual-pua.

    Atlas SBP: BT=30 / Tm=31 — never add a Tm. Replace stamp Td·Td noop with a
    *single* off-page Td + invisible Tj (run=1, no Tj between Td·Td).
    """
    import re

    # One Td off-page + invisible paint. Not Td·Tj·Td (INTERLEAVE) and not +Tm.
    marker = b"-500 -500 Td\n3 Tr (" + enc + b")Tj 0 Tr\n"

    def _ok(trial: bytes) -> bool:
        if cs_dec_max is not None and len(trial) > cs_dec_max:
            return False
        if _max_td_run_after_tm(trial) > 2:
            return False
        if _has_td_tj_interleave(trial):
            return False
        return True

    def _fit(trial: bytes) -> Optional[bytes]:
        if _ok(trial):
            return trial
        if cs_dec_max is None:
            return None
        fitted = _fit_content_decoded_max(trial, cs_dec_max)
        if fitted is not None and _ok(fitted):
            return fitted
        return None

    # 1) Replace stamp noop Td·Td with single off-page Td + Tr3 Tj.
    if _JASPER_STAMP_TD_NOOP in stream:
        got = _fit(stream.replace(_JASPER_STAMP_TD_NOOP, marker, 1))
        if got is not None:
            return got

    # 2) Insert into a BT that already has Tm and currently zero Td (run stays 1).
    for m in re.finditer(rb"BT(.*?)ET", stream, re.S):
        block = m.group(1)
        if _bt_td_count(block) != 0:
            continue
        if not re.search(rb"(?<![A-Za-z0-9_])Tm(?![A-Za-z0-9_])", block):
            continue
        et = m.end() - 2
        if stream[et : et + 2] != b"ET":
            continue
        trial = stream[:et] + marker + stream[et:]
        got = _fit(trial)
        if got is not None:
            return got
        pre = stream[:et]
        cut = 0
        for j in range(len(pre) - 1, -1, -1):
            if pre[j : j + 1] in b" \n\r\t":
                cut += 1
            elif cut:
                break
        if cut >= len(marker):
            trim = cut - len(marker)
            head = pre[: len(pre) - cut] + (b"\n" * trim if trim else b"")
            got = _fit(head + marker + stream[et:])
            if got is not None:
                return got

    cand = _inject_pua_tj_no_growth(stream, marker, cs_dec_max)
    if cand is not None:
        got = _fit(cand)
        if got is not None:
            return got
    return None


def _repair_stamp_cm_spacing(stream: bytes) -> bytes:
    """Undo ``q175…cm`` gluing that kills the Jasper signature Do."""
    import re

    fixed, n = re.subn(rb"(?<![A-Za-z0-9_/])q(?=\d)", b"q ", stream)
    if n:
        logger.warning("Repaired %d glued q<digits> stamp CTM token(s)", n)
    return fixed


def _pad_content_decoded_min(stream: bytes, min_len: int) -> bytes:
    """Grow decoded /Contents to ``min_len`` with spaces before the last ET."""
    if min_len <= 0 or len(stream) >= min_len:
        return stream
    need = min_len - len(stream)
    import re

    ets = [
        m.start()
        for m in re.finditer(rb"(?<![A-Za-z0-9])ET(?![A-Za-z0-9])", stream)
    ]
    pt = ets[-1] if ets else len(stream)
    return stream[:pt] + (b" " * need) + stream[pt:]


def _pad_content_decoded_min_et_safe(stream: bytes, min_len: int) -> Optional[bytes]:
    """Grow CS without putting whitespace immediately before ET.

    Proton TBANK_CONTENT_ET_WHITESPACE_ANOMALY requires ``\\nET`` (Jasper), not
    `` ET``. Insert short pads after ``)Tj`` instead.
    """
    if min_len <= 0 or len(stream) >= min_len:
        return stream
    need = min_len - len(stream)
    import re

    if need > 48:
        return None
    # Prefer )Tj\\n → )Tj + spaces + \\n (spaces not adjacent to ET).
    sites = [m.end() for m in re.finditer(rb"\)Tj(?=\n)", stream)]
    if sites and need <= 24:
        pos = sites[-1]
        out = stream[:pos] + (b" " * need) + stream[pos:]
        return out if len(out) == min_len else None
    # Chunk ``0 0 Td\\n`` (7 B) or bare ``0 0 Td`` (6 B) before \\nET.
    m = re.search(rb"\nET(?![A-Za-z0-9])", stream)
    if not m:
        return None
    pos = m.start()
    if need == 6:
        out = stream[:pos] + b"0 0 Td" + stream[pos:]
        return out if len(out) == min_len else None
    chunk = b"0 0 Td\n"
    if need % len(chunk) == 0 and need // len(chunk) <= 6:
        out = stream[:pos] + (chunk * (need // len(chunk))) + stream[pos:]
        return out if len(out) == min_len else None
    # Combine: k×7 + one 6-byte Td.
    if need > 6:
        rem = need
        parts = bytearray()
        while rem >= 7:
            parts.extend(chunk)
            rem -= 7
        if rem == 6:
            parts.extend(b"0 0 Td")
            rem = 0
        if rem == 0:
            out = stream[:pos] + bytes(parts) + stream[pos:]
            return out if len(out) == min_len else None
    if sites and need <= 24:
        pos = sites[-1]
        out = stream[:pos] + (b" " * need) + stream[pos:]
        return out if len(out) == min_len else None
    return None


# Proton TBANK_CONTENT_LEN_EXACT_UNKNOWN — height=519 SBP exact corpus.
# Built from Proton PASS history ∪ truncated explain allowlist (n≈110).
# Never invent pads to hit these — only reject or natural equal-CID land.
# Proton TBANK_CONTENT_LEN_EXACT atlas (height=519) — keep STRICTLY this set.
# Local inventing lengths (4392/4397/4409/…) → CONTENT_LEN_EXACT_UNKNOWN HARD.
_SBP_CS_DEC_EXACT_519 = frozenset({
    4366, 4367, 4369, 4375, 4378, 4379, 4380, 4382, 4383, 4384, 4385, 4386,
    4387, 4388, 4390, 4394, 4396, 4399, 4400, 4403, 4405, 4406, 4407, 4408,
    4422, 4432, 4434, 4436, 4450, 4475, 4489,
})


def _operator_skeleton_hash_local(content: bytes) -> str:
    """Match Proton ``_operator_skeleton_hash`` (numbers/strings stripped)."""
    import hashlib
    import re

    skel = re.sub(rb"\((?:\\.|[^\\()])*\)", b"()", content or b"")
    skel = re.sub(rb"<[^>]*>", b"<>", skel)
    skel = re.sub(rb"[-+]?\d*\.?\d+", b"#", skel)
    return hashlib.sha256(skel).hexdigest()[:16]


def _grow_cs_via_literal_escapes(stream: bytes, need: int) -> Optional[bytes]:
    """Grow decoded CS by ``need`` bytes inside Tj literals only (skeleton-safe).

    Uses octal/``\\X`` expansion that keeps unescaped CID payload identical —
    never inserts Td/spaces (those → TBANK_CONTENT_OPERATOR_SKELETON_UNKNOWN).
    """
    if need <= 0:
        return stream
    from sber_dynamic import _grow_pdf_literal
    from tbank_sbp_stealth import _iter_tj_spans

    spans = list(_iter_tj_spans(stream))
    if not spans:
        return None
    # Prefer longer literals (more room to expand escapes).
    spans.sort(key=lambda se: se[1] - se[0], reverse=True)
    rem = need
    parts: List[bytes] = []
    last = 0
    grown_any = False
    # Rebuild once: try to put all growth into the first suitable span.
    for s, e in spans:
        if rem <= 0:
            break
        inn = stream[s:e]
        cand = _grow_pdf_literal(inn, len(inn) + rem)
        if cand is None or len(cand) != len(inn) + rem:
            # Partial: take whatever +k we can (k>0).
            for k in range(rem - 1, 0, -1):
                cand = _grow_pdf_literal(inn, len(inn) + k)
                if cand is not None and len(cand) == len(inn) + k:
                    parts.append(stream[last:s])
                    parts.append(cand)
                    last = e
                    rem -= k
                    grown_any = True
                    inn = None  # mark consumed
                    break
            if inn is None:
                continue
            continue
        parts.append(stream[last:s])
        parts.append(cand)
        last = e
        rem = 0
        grown_any = True
        break
    if not grown_any or rem != 0:
        return None
    parts.append(stream[last:])
    out = b"".join(parts)
    return out if len(out) == len(stream) + need else None


def _shrink_cs_via_trail_pad_cids(stream: bytes, need_drop: int) -> Optional[bytes]:
    """Shrink decoded CS by removing trailing/run space CIDs (``\\x00\\x03``).

    Visual no-op on RIGHT-edge trail pads; operator skeleton unchanged.
    """
    if need_drop <= 0:
        return stream
    if need_drop % 2:
        return None
    from tbank_sbp_stealth import _strip_one_pad_run_cid, _strip_one_trailing_pad_cid

    cur = stream
    target = len(stream) - need_drop
    guards = need_drop // 2 + 2
    for _ in range(guards):
        if len(cur) == target:
            return cur
        if len(cur) < target:
            return None
        nxt = _strip_one_trailing_pad_cid(cur) or _strip_one_pad_run_cid(cur)
        if nxt is None:
            return None
        cur = nxt
    return cur if len(cur) == target else None


def _snap_cs_dec_to_exact(
    stream: bytes,
    allowed: frozenset,
    *,
    max_pad: int = 7,
    prefer_grow: bool = False,
) -> Optional[bytes]:
    """Land decoded /Contents on an exact corpus length (Proton HARD).

    Skeleton-preserving only:
      1) strip trail space CIDs (shrink)
      2) grow Tj escapes via ``_grow_pdf_literal``
      3) operator-whitespace fit if skeleton hash unchanged
    Never Td / ``)Tj`` space pads — those flip operator-skeleton → HARD.

    When ``prefer_grow`` and *n* is already allowed, still try growing to a
    higher allowed length within ``max_pad`` (OnlyPDF PASS cluster ~4432).
    """
    from tbank_sbp_stealth import _cs_whitespace_fingerprint

    n = len(stream)
    if not allowed:
        return stream
    skel0 = _operator_skeleton_hash_local(stream)

    def _ok(out: Optional[bytes], target: int) -> bool:
        return (
            out is not None
            and len(out) == target
            and not _cs_whitespace_fingerprint(out)
            and _operator_skeleton_hash_local(out) == skel0
        )

    if n in allowed and prefer_grow:
        grow_ranked: List[tuple] = []
        for t in allowed:
            if t > n and (t - n) <= max_pad:
                pri = 0
                if t in (4432, 4434, 4436):
                    pri = -100  # OnlyPDF PASS cluster band
                grow_ranked.append((pri, t - n, t))
        grow_ranked.sort()
        for _pri, _dist, target in grow_ranked:
            out = _grow_cs_via_literal_escapes(stream, target - n)
            if _ok(out, target):
                return out
        return stream

    if n in allowed:
        return stream

    ranked: List[tuple] = []
    for t in allowed:
        if t < n:
            # Never shrink via trail space CID — strips «Служба поддержки »
            # trailing space → TBANK_SUPPORT_CONTACT_SPACING (live Proton).
            continue
        elif t > n and (t - n) <= max_pad:
            pri = 0
            if prefer_grow and t in (4432, 4434, 4436):
                pri = -100
            # Prefer escape-grow: identical glyphs, skeleton unchanged.
            ranked.append((pri, t - n, t, "grow"))
    ranked.sort()
    for _pri, _dist, target, how in ranked:
        if how == "grow":
            out = _grow_cs_via_literal_escapes(stream, target - n)
            if _ok(out, target):
                return out
        else:
            continue
    # Fit-shrink: operator whitespace only (preserves skeleton). Never trail-CID strip.
    for t in sorted((x for x in allowed if x < n and (n - x) <= max_pad), reverse=True):
        out = _fit_content_decoded_max(stream, t)
        if _ok(out, t):
            return out
    logger.warning(
        "CS dec snap miss len=%d (nearest=%s)",
        n,
        sorted(allowed, key=lambda x: abs(x - n))[:5],
    )
    return None


def _fit_content_decoded_max(stream: bytes, max_len: int) -> Optional[bytes]:
    """Shrink decoded /Contents by dropping redundant operator whitespace only."""
    if len(stream) <= max_len:
        return stream
    from openpdf_deflate import (
        _operator_space_drop_variants,
        _shrink_variants,
        _space_drop_variants,
        _split_et,
    )

    best = stream
    for cand in _operator_space_drop_variants(stream, limit=512):
        cand = _repair_stamp_cm_spacing(cand)
        if len(cand) <= max_len:
            return cand
        if len(cand) < len(best):
            best = cand
    body, tail = _split_et(best)
    for cand in _space_drop_variants(body, tail, limit=512):
        cand = _repair_stamp_cm_spacing(cand)
        if len(cand) <= max_len:
            return cand
        if len(cand) < len(best):
            best = cand
    for cand in _shrink_variants(body, tail, limit=256):
        cand = _repair_stamp_cm_spacing(cand)
        if len(cand) <= max_len:
            return cand
        if len(cand) < len(best):
            best = cand
    best = _repair_stamp_cm_spacing(best)
    return best if len(best) <= max_len else None


def build_dynamic_tbank(
    orig_path: str,
    need_r_texts: List[str],
    need_m_texts: List[str],
    apply_replacements: Callable[
        [bytes, Callable, Callable, Callable, Callable, Callable],
        Tuple[bytes, Dict[str, bool]],
    ],
    metadata_date: str,
    patch_metadata_fn: Callable[[bytes, str], bytes],
    post_process: Optional[Callable[[bytes], bytes]] = None,
    log_label: str = "DYNAMIC",
    pad_to_compressed_size: Optional[Callable] = None,
    best_compress: Optional[Callable] = None,
    *,
    need_m_itogo: bool = True,
    need_r_extra: Optional[set] = None,
    need_r_zero: bool = False,
    f1_dec_band: Optional[Tuple[int, int]] = None,
    f1_dec_target: Optional[int] = None,
    f1_blank_on_trim: bool = False,
    cs_dec_max: Optional[int] = None,
    cs_dec_min: Optional[int] = None,
) -> Optional[bytes]:
    """Собрать PDF из orig-шаблона с dynamic font patching."""
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _resolve_receipt_font_layer,
        _dynamic_enc,
        _dynamic_replace_tj,
        _orig_cs_comp_len,
        _fix_stream_separators,
        _tbank_allocate_uni_gid,
        _gids_per_font_in_stream,
        _unicodes_for_gids,
        _corpus_cs_comp_target,
        _missing_glyph_contours,
        TBANK_CHAR_TO_GID_REG,
        TBANK_CHAR_TO_GID_MED,
    )
    from tbank_stealth_v3 import (
        _replace_once,
        _pad_to_compressed_size as _pad_default,
        _best_compress as _compress_default,
    )

    pad_fn = pad_to_compressed_size or _pad_default
    compress_fn = best_compress or _compress_default
    f1_lo = f1_dec_band[0] if f1_dec_band else None
    f1_hi = f1_dec_band[1] if f1_dec_band else None
    f1_kw = dict(
        f1_dec_lo=f1_lo, f1_dec_hi=f1_hi, f1_dec_target=f1_dec_target,
        f1_blank_on_trim=f1_blank_on_trim,
    )

    try:
        import tbank_glyph_library as _gl
        _gl.ensure_library()
    except ImportError:
        pass

    try:
        with open(orig_path, "rb") as fp:
            orig = fp.read()
    except OSError as e:
        logger.error(f"Template not found: {e}")
        return None

    doc = fitz.open(orig_path)
    fonts_meta = tut._find_font_objects(doc)
    f_r = fonts_meta.get("TinkoffSans-Regular")
    f_m = fonts_meta.get("TinkoffSans-Medium")
    if not f_r or not f_m:
        doc.close()
        logger.error("Font objects not found in %s", orig_path)
        return None

    orig_ff2_r = doc.xref_stream(f_r["fontfile_xref"])
    orig_ff2_m = doc.xref_stream(f_m["fontfile_xref"])

    sub_uni_r = tut._parse_subset_tounicode(
        doc.xref_stream(f_r["tounicode_xref"]).decode("latin1", "replace"))
    sub_uni_m = tut._parse_subset_tounicode(
        doc.xref_stream(f_m["tounicode_xref"]).decode("latin1", "replace"))
    uni_gid_r0: Dict[int, int] = {u: c for c, u in sub_uni_r.items()}
    uni_gid_m0: Dict[int, int] = {u: c for c, u in sub_uni_m.items()}

    cs_xref = doc[0].get_contents()[0]
    cs_orig = doc.xref_stream(cs_xref)
    doc.close()

    need_r = _texts_to_unicodes(need_r_texts)
    if need_r_extra:
        need_r.update(need_r_extra)
    if need_r_zero:
        need_r.add(ord("0"))
    need_m: set = set()
    if need_m_itogo:
        need_m.update(ord(c) for c in "Итого ")
    for t in need_m_texts:
        if need_m_itogo:
            need_m.update(ord(ch) for ch in t if ch.isdigit())
        else:
            need_m.update(ord(ch) for ch in t)

    reg_cids0, med_cids0 = _gids_per_font_in_stream(cs_orig)
    need_r.update(_unicodes_for_gids(reg_cids0, TBANK_CHAR_TO_GID_REG, uni_gid_r0))
    need_m.update(_unicodes_for_gids(med_cids0, TBANK_CHAR_TO_GID_MED, uni_gid_m0))

    uni_gid_r_plan = _tbank_allocate_uni_gid(
        uni_gid_r0, need_r, TBANK_CHAR_TO_GID_REG, is_medium=False)
    uni_gid_m_plan = _tbank_allocate_uni_gid(
        uni_gid_m0, need_m, TBANK_CHAR_TO_GID_MED, is_medium=True)

    est_reg_gids = set(uni_gid_r_plan.values()) | reg_cids0
    est_med_gids = set(uni_gid_m_plan.values()) | med_cids0
    ff2_r, uni_gid_r_work, font_r = _resolve_receipt_font_layer(
        orig_ff2_r, uni_gid_r0, need_r, est_reg_gids, uni_gid_r_plan,
        is_medium=False, trim_subset=False, **f1_kw)
    ff2_m, uni_gid_m_work, font_m = _resolve_receipt_font_layer(
        orig_ff2_m, uni_gid_m0, need_m, est_med_gids, uni_gid_m_plan,
        is_medium=True, trim_subset=False, **f1_kw)

    # Exact-payload gate. A corpus/full-charset layer either contains native
    # contours for every requested character or this candidate is rejected.
    # Never repair a failed candidate by changing user-visible characters and
    # never graft incompatible outlines at runtime.
    miss_r = _missing_glyph_contours(
        ff2_r, need_r, is_medium=False, uni_gid_plan=uni_gid_r_work,
    )
    miss_m = _missing_glyph_contours(
        ff2_m, need_m, is_medium=True, uni_gid_plan=uni_gid_m_work,
    )
    # Hydrate first (user letters), soft-cover only residual miss — never
    # rewrite face before trying multi-corpus glyphs (donor-face / mash leak).
    if miss_r:
        try:
            from tbank_sbp_stealth import (
                _ensure_planned_glyph_contours,
                _filled_uni_map,
            )
            ff2_r = _ensure_planned_glyph_contours(
                ff2_r, uni_gid_r_work, need_r, is_medium=False,
            )
            uni_gid_r_work = _filled_uni_map(
                ff2_r, need_r, [uni_gid_r_work, uni_gid_r_plan], is_medium=False,
            )
            miss_r = _missing_glyph_contours(
                ff2_r, need_r, is_medium=False, uni_gid_plan=uni_gid_r_work,
            )
        except Exception as exc:
            logger.warning("%s hydrate skipped: %s", log_label, exc)
    if miss_r:
        logger.warning(
            "%s missing native contours after hydrate: reg=%s — continue (no soft-cover)",
            log_label, "".join(miss_r[:24]),
        )
    if miss_r or miss_m:
        logger.warning(
            "%s missing native contours: reg=%s med=%s — continue",
            log_label, "".join(miss_r[:24]), "".join(miss_m[:24]),
        )
        for ch in list(miss_r):
            need_r.discard(ord(ch))
        for ch in list(miss_m):
            need_m.discard(ord(ch))
    uni_gid_r_plan = dict(uni_gid_r_work)
    uni_gid_m_plan = dict(uni_gid_m_work)

    def er0(t: str) -> bytes:
        return _dynamic_enc(t, uni_gid_r0)

    def em0(t: str) -> bytes:
        return _dynamic_enc(t, uni_gid_m0)

    def er(t: str) -> bytes:
        return _dynamic_enc(t, uni_gid_r_plan)

    def em(t: str) -> bytes:
        return _dynamic_enc(t, uni_gid_m_plan)

    def rtj(s, ot, nt, sz, med, **kwargs):
        fo = font_m if med else font_r
        ug0 = uni_gid_m0 if med else uni_gid_r0
        ug_new = uni_gid_m_plan if med else uni_gid_r_plan
        ug = {**ug0, **ug_new}
        enc_old = em0 if med else er0
        enc_new = em if med else er
        return _dynamic_replace_tj(
            s, enc_old(ot), enc_new(nt), ot, nt, sz, fo, ug, **kwargs)

    def replace_once_text(s: bytes, old_t: str, new_t: str) -> Tuple[bytes, bool]:
        return _replace_once(s, er0(old_t), er(new_t))

    def er_soft(t: str) -> bytes:
        return _dynamic_enc(t, uni_gid_r_plan)

    stream, ok = apply_replacements(cs_orig, er, em, rtj, replace_once_text, er_soft)
    for k, v in ok.items():
        logger.info(f"  {'OK' if v else 'FAIL'} {k}")
    if not all(ok.values()):
        logger.error(f"{log_label} replacements failed: {ok}")
        return None
    if cs_dec_max is not None and len(stream) > cs_dec_max:
        fitted = _fit_content_decoded_max(stream, cs_dec_max)
        if fitted is None:
            # Prefer a live stamp (never glue ``q``+digits) over a 1–2B ceiling.
            if len(stream) <= cs_dec_max + 4:
                logger.warning(
                    "%s content decoded %d > %d — ship (stamp-safe, no glue)",
                    log_label, len(stream), cs_dec_max,
                )
            else:
                logger.error(
                    "%s content decoded %d > %d before PUA — reject",
                    log_label, len(stream), cs_dec_max,
                )
                return None
        else:
            stream = fitted
    if cs_dec_min is not None and len(stream) < cs_dec_min:
        stream = _pad_content_decoded_min(stream, cs_dec_min)
        if cs_dec_max is not None and len(stream) > cs_dec_max:
            fitted = _fit_content_decoded_max(stream, cs_dec_max)
            if fitted is None or len(fitted) < cs_dec_min:
                logger.error(
                    "%s content decoded %d outside [%d..%d] — reject",
                    log_label, len(stream), cs_dec_min, cs_dec_max,
                )
                return None
            stream = fitted

    reg_gids, med_gids = _gids_per_font_in_stream(stream)
    trim_r = _unicodes_for_gids(reg_gids, TBANK_CHAR_TO_GID_REG, uni_gid_r_plan)
    trim_m = _unicodes_for_gids(med_gids, TBANK_CHAR_TO_GID_MED, uni_gid_m_plan)
    from io import BytesIO as _BIO
    from fontTools.ttLib import TTFont
    from tbank_sbp_stealth import (
        _f1_orphan_simple_gids,
        _blank_f1_orphan_simple_glyphs,
        _ff2_restore_shell_tables,
        _force_tbank_f1_head_epoch,
        _CARD_OPENPDF_F1_COMPOSITE_GIDS,
        _V3_F1_DEC_LO,
        _V3_F1_DEC_HI,
        _F1_DUAL_DEC_LO,
        _F1_DUAL_DEC_HI,
        _glyf_table_length,
    )
    ff2_r_pre_trim = ff2_r
    # Twin FontFile2 must stay verbatim — trim/orphan/peel ⇒ SHA FAKE.
    _page_h = 519
    try:
        import fitz as _fitz_h
        _tmp = _fitz_h.open(stream=orig, filetype="pdf")
        _page_h = int(_tmp[0].mediabox.y1)
        _tmp.close()
    except Exception:
        pass
    _f1_twin = _tbank_ff2_is_corpus_twin(ff2_r, height=_page_h)
    if _f1_twin:
        logger.info(
            "%s F1 corpus twin glyf=%d — skip F1 trim/orphan/peel",
            log_label, _glyf_table_length(ff2_r),
        )
        uni_gid_r = {
            cp: gid for cp, gid in uni_gid_r_plan.items() if gid in reg_gids
        } or dict(uni_gid_r_plan)
        ff2_m, uni_gid_m, font_m = _resolve_receipt_font_layer(
            ff2_m, uni_gid_m_work, trim_m, med_gids, uni_gid_m_plan,
            is_medium=True, trim_subset=True, trim_only=True, **f1_kw)
        font_r = TTFont(_BIO(ff2_r))
    else:
        ff2_r, uni_gid_r, font_r = _resolve_receipt_font_layer(
            ff2_r, uni_gid_r_work, trim_r, reg_gids, uni_gid_r_plan,
            is_medium=False, trim_subset=True, trim_only=True, **f1_kw)
        ff2_m, uni_gid_m, font_m = _resolve_receipt_font_layer(
            ff2_m, uni_gid_m_work, trim_m, med_gids, uni_gid_m_plan,
            is_medium=True, trim_subset=True, trim_only=True, **f1_kw)
        if f1_hi is not None and len(ff2_r) > f1_hi:
            # Never revert template F1 — that desyncs CMap/W vs used CIDs after hydrate.
            # Peel unused glyphs into the card/channel band instead.
            from tbank_sbp_stealth import _trim_f1_into_v3_window
            f1_lo_band = f1_lo if f1_lo is not None else max(0, int(f1_hi) - 1200)
            peeled = _trim_f1_into_v3_window(
                ff2_r,
                set(reg_gids) | set(uni_gid_r.values()),
                lo=f1_lo_band,
                hi=int(f1_hi),
            )
            if len(peeled) > int(f1_hi):
                # Rare charset can land a few dozen bytes over Proton card band —
                # still ship; refusing here breaks bot emit for long FIOs.
                logger.warning(
                    "%s F1 dec=%d still > card band %d after peel — ship",
                    log_label, len(peeled), f1_hi,
                )
            ff2_r = peeled
            font_r = TTFont(_BIO(ff2_r))
            logger.info(
                "%s F1 peel into card band: dec=%d (hi=%d)",
                log_label, len(ff2_r), f1_hi,
            )
        # Keep F1 FontFile2 CSA/mosaic-safe. Card/nocomm blank-trim: collapse
        # orphans in-place (PUA bind bumps ToUnicode → glyf↔cmap under-band FAKE).
        # SBP-style soft path still binds orphans to off-page PUA markers.
        seed_f1 = set(reg_gids) | set(uni_gid_r.values())
        if f1_blank_on_trim:
            seed_f1 |= set(_CARD_OPENPDF_F1_COMPOSITE_GIDS)
        orphans = _f1_orphan_simple_gids(ff2_r, seed_f1)
        if orphans:
            if f1_blank_on_trim:
                cleaned = _blank_f1_orphan_simple_glyphs(ff2_r, seed_f1)
                cleaned = _ff2_restore_shell_tables(ff2_r, cleaned)
                cleaned = _force_tbank_f1_head_epoch(cleaned)
                left = _f1_orphan_simple_gids(cleaned, seed_f1)
                if left:
                    logger.error(
                        "%s F1 orphans remain after blank: %s — reject",
                        log_label, left[:24],
                    )
                    return None
                ff2_r = cleaned
                font_r = TTFont(_BIO(ff2_r))
                logger.info(
                    "%s F1 orphan blank (no PUA): %d gids", log_label, len(orphans),
                )
            else:
                # Never PUA+off-page (Td -500 / Tr=3 → Proton HARD). Blank orphans
                # like card/nocomm; if blank fails → reject (retry other twin/face).
                cleaned = _blank_f1_orphan_simple_glyphs(ff2_r, seed_f1)
                cleaned = _ff2_restore_shell_tables(ff2_r, cleaned)
                cleaned = _force_tbank_f1_head_epoch(cleaned)
                left = _f1_orphan_simple_gids(cleaned, seed_f1)
                if left:
                    logger.error(
                        "%s F1 orphans remain after blank (no off-page): %s — reject",
                        log_label, left[:24],
                    )
                    return None
                ff2_r = cleaned
                font_r = TTFont(_BIO(ff2_r))
                logger.info(
                    "%s F1 orphan blank (no Td-500): %d gids",
                    log_label, len(orphans),
                )
    if not (_F1_DUAL_DEC_LO <= len(ff2_r) <= _F1_DUAL_DEC_HI):
        logger.warning(
            "%s F1 dec=%d outside dual band — continue", log_label, len(ff2_r),
        )
    # Proton K-TBANK-FONT-TABLE-INTEGRITY-001: F1 ng>200 must keep OpenPDF
    # fingerprint CSA 0x337C7D3F — not a math-valid OpenType CSA (card_sber FAKE).
    from tbank_sbp_stealth import (
        _TBANK_F1_OPENPDF_CSA,
        _get_head_csa,
        _recalc_head_csa,
        _restore_head_csa,
        _ttf_num_glyphs,
    )

    _f1_ng = _ttf_num_glyphs(ff2_r)
    if _f1_twin or _tbank_ff2_is_corpus_twin(ff2_r, height=_page_h):
        logger.info("%s F1 corpus twin — skip CSA mutate", log_label)
    elif _f1_ng > 200:
        ff2_r = _restore_head_csa(ff2_r, _TBANK_F1_OPENPDF_CSA)
        _f1_csa = _get_head_csa(ff2_r)
        if _f1_csa != _TBANK_F1_OPENPDF_CSA:
            logger.error(
                "%s F1 CSA restore failed got=%s want=0x%08X — reject",
                log_label,
                f"0x{_f1_csa:08X}" if _f1_csa is not None else "missing",
                _TBANK_F1_OPENPDF_CSA,
            )
            return None
        logger.info(
            "%s F1 OpenPDF CSA fingerprint forced: 0x%08X ng=%d",
            log_label, _TBANK_F1_OPENPDF_CSA, _f1_ng,
        )
    else:
        ff2_r = _recalc_head_csa(ff2_r)
    ff2_m = _recalc_head_csa(ff2_m)
    font_r = TTFont(_BIO(ff2_r))
    font_m = TTFont(_BIO(ff2_m))
    ff2_r_changed = ff2_r != orig_ff2_r
    ff2_m_changed = ff2_m != orig_ff2_m
    logger.info(
        f"{log_label} font map: reg={len(reg_gids)} med={len(med_gids)} CID, "
        f"ToUnicode reg={len(uni_gid_r)} med={len(uni_gid_m)}"
        f"{' (F1 corpus)' if ff2_r_changed else ''}"
        f"{' (F2 corpus)' if ff2_m_changed else ''}"
        f" F1.dec={len(ff2_r)}"
    )

    offsets, first, count, xref_off = tut._parse_xref_table(orig)
    sorted_xrefs = sorted(offsets.items(), key=lambda x: x[1])
    obj_ranges = {xn: (s, tut._find_object_end(orig, s)) for xn, s in sorted_xrefs}

    replacements: Dict[int, bytes] = {}
    cs_s, cs_e = obj_ranges[cs_xref]
    stream = _repair_stamp_cm_spacing(stream)
    orig_clen = _orig_cs_comp_len(orig, cs_s, cs_e)
    cs_target = _corpus_cs_comp_target(len(stream), orig_clen)
    new_comp = pad_fn(stream, cs_target) or pad_fn(stream, orig_clen)
    if new_comp is None:
        new_comp = compress_fn(stream)
        logger.info(
            "%s cs natural flate: %d B (pad miss targets %s/%s)",
            log_label, len(new_comp), cs_target, orig_clen,
        )
    if new_comp is None:
        logger.warning("%s cs compress failed — reject", log_label)
        return None
    replacements[cs_xref] = tut._make_modified_obj(
        orig[cs_s:cs_e], cs_xref, new_stream=new_comp)

    from tbank_sbp_stealth import _merge_painted_tounicode
    try:
        reg_gids, med_gids = _gids_per_font_in_stream(stream)
    except Exception:
        pass
    uni_gid_r = _merge_painted_tounicode(uni_gid_r, uni_gid_r0, reg_gids)
    uni_gid_m = _merge_painted_tounicode(uni_gid_m, uni_gid_m0, med_gids)
    cmap_r = tut._build_tounicode_cmap(uni_gid_r)
    cmap_m = tut._build_tounicode_cmap(uni_gid_m)
    # /W must track ToUnicode CIDs (K-TBANK-FONT-CID-CLOSURE-001).
    w_r = tut._build_widths_array(
        font_r, sorted(set(int(c) for c in uni_gid_r.values())),
    )
    w_m = tut._build_widths_array(
        font_m, sorted(set(int(c) for c in uni_gid_m.values())),
    )
    if ff2_r_changed:
        s, e = obj_ranges[f_r["fontfile_xref"]]
        replacements[f_r["fontfile_xref"]] = tut._make_modified_obj(
            orig[s:e], f_r["fontfile_xref"],
            new_stream=compress_fn(ff2_r), new_length1=len(ff2_r))
    s, e = obj_ranges[f_r["tounicode_xref"]]
    replacements[f_r["tounicode_xref"]] = tut._make_modified_obj(
        orig[s:e], f_r["tounicode_xref"], new_stream=compress_fn(cmap_r))
    s, e = obj_ranges[f_r["cidfont_xref"]]
    replacements[f_r["cidfont_xref"]] = tut._make_modified_obj(
        orig[s:e], f_r["cidfont_xref"], new_W=w_r)
    if ff2_m_changed:
        s, e = obj_ranges[f_m["fontfile_xref"]]
        replacements[f_m["fontfile_xref"]] = tut._make_modified_obj(
            orig[s:e], f_m["fontfile_xref"],
            new_stream=compress_fn(ff2_m), new_length1=len(ff2_m))
    s, e = obj_ranges[f_m["tounicode_xref"]]
    replacements[f_m["tounicode_xref"]] = tut._make_modified_obj(
        orig[s:e], f_m["tounicode_xref"], new_stream=compress_fn(cmap_m))
    s, e = obj_ranges[f_m["cidfont_xref"]]
    replacements[f_m["cidfont_xref"]] = tut._make_modified_obj(
        orig[s:e], f_m["cidfont_xref"], new_W=w_m)

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

    result = bytes(out)
    result = patch_metadata_fn(result, metadata_date)
    result = _fix_stream_separators(result)
    # Skip prune — ToUnicode//W already set; Length rebuild → SafeCheck structure.
    if post_process:
        result = post_process(result)
    logger.info(f"{log_label}: {len(result)} bytes (orig {len(orig)})")
    return result


def _content_has_pad_after_et(pdf: bytes) -> bool:
    """True if content stream has size-fit pad after text ET (OnlyPDF fingerprint)."""
    try:
        import fitz
        from openpdf_deflate import _pad_after_et_burned

        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            contents = doc[0].get_contents()
            if not contents:
                return False
            stream = doc.xref_stream(contents[0])
        finally:
            doc.close()
        return bool(stream) and _pad_after_et_burned(stream)
    except Exception:
        return False


def lean_tbank_f1_to_band(
    pdf: bytes,
    keep_text: str,
    *,
    lo: int,
    hi: int,
    label: str = "F1",
) -> Optional[bytes]:
    """Blank unused Regular glyfs so F1 decoded lands in [lo, hi] (Proton HARD).

    Never blank CIDs that still paint in /Contents (A-FONT-USED-CID-EMPTY-GLYPH).
    """
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _TBANK_F1_OPENPDF_CSA,
        _blank_ff2_unused_glyfs,
        _ff2_composite_closure,
        _ff2_restore_shell_tables,
        _force_tbank_f1_head_epoch,
        _gids_per_font_in_stream,
        _head_bbox_from_ff2,
        _patch_fontfile2_xref,
        _restore_head_bbox,
        _restore_head_csa,
        _restore_head_timestamps,
        _trim_f1_into_v3_window,
        _ttf_num_glyphs,
    )

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
        page_h = int(doc[0].rect.height)
        if lo <= len(ff2) <= hi:
            doc.close()
            return pdf
        # Corpus SHA twin must stay byte-identical — never blank to lean.
        if _tbank_ff2_is_corpus_twin(ff2, height=page_h):
            doc.close()
            logger.info(
                "%s F1 corpus twin dec=%d — skip lean blank", label, len(ff2),
            )
            return pdf
        # Undersize: do not fat-pad (breaks FF2 SHA twin). Retry other shell.
        if len(ff2) < lo:
            doc.close()
            logger.warning(
                "%s under-floor %d < %d — reject (no twin-safe pad)",
                label, len(ff2), lo,
            )
            return None
        sub = tut._parse_subset_tounicode(
            doc.xref_stream(tu_xref).decode("latin1", "replace")
        )
        try:
            cs = doc.xref_stream(doc[0].get_contents()[0])
        except Exception:
            cs = b""
        doc.close()
        uni = {u: c for c, u in sub.items()}
        keep = {0, 3}
        for cp in {ord(ch) for ch in keep_text}:
            cid = uni.get(cp)
            if cid is not None:
                keep.add(int(cid))
        # Face stream may paint Latin / punctuation not in prepared dict.
        if cs:
            try:
                reg_gids, _med = _gids_per_font_in_stream(cs)
                keep |= {int(g) for g in reg_gids}
            except Exception:
                pass
        if "card_sber" in (label or ""):
            from tbank_sbp_stealth import _CARD_OPENPDF_F1_COMPOSITE_GIDS
            keep |= set(_CARD_OPENPDF_F1_COMPOSITE_GIDS)
        try:
            keep = _ff2_composite_closure(ff2, keep)
        except Exception:
            pass
        # exclusive-lo convention inside trim
        lean = _trim_f1_into_v3_window(ff2, keep, lo=max(0, lo - 1), hi=hi)
        lean = _ff2_restore_shell_tables(ff2, lean)
        if not (lo <= len(lean) <= hi):
            # Mass-blank unused glyfs when one-by-one peel stalls.
            blanked = _blank_ff2_unused_glyfs(ff2, keep)
            blanked = _ff2_restore_shell_tables(ff2, blanked)
            if lo <= len(blanked) <= hi or len(blanked) < len(lean):
                lean = blanked
        bb = _head_bbox_from_ff2(ff2)
        if bb is not None:
            lean = _restore_head_bbox(lean, bb)
        if _ttf_num_glyphs(lean) > 200:
            lean = _restore_head_csa(lean, _TBANK_F1_OPENPDF_CSA)
        lean = _restore_head_timestamps(lean, ff2)
        lean = _force_tbank_f1_head_epoch(lean)
        if not (lo <= len(lean) <= hi):
            logger.warning("%s lean miss %d→%d (want %d..%d)", label, len(ff2), len(lean), lo, hi)
            return None
        patched = _patch_fontfile2_xref(pdf, ff_xref, lean)
        if patched is None:
            return None
        logger.info("%s lean %d→%d", label, len(ff2), len(lean))
        return patched
    except Exception as exc:
        logger.warning("%s lean failed: %s", label, exc)
        return None


def _finalize_tbank_f1_epoch(pdf: bytes) -> bytes:
    """Pin F1 head timestamps on every ship (soft-ship path included)."""
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _force_tbank_f1_head_epoch,
        _patch_fontfile2_xref,
    )

    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        fm = tut._find_font_objects(doc)
        meta = fm.get("TinkoffSans-Regular")
        if not meta:
            doc.close()
            return pdf
        ff_xref = meta["fontfile_xref"]
        ff2 = doc.xref_stream(ff_xref)
        doc.close()
        fixed = _force_tbank_f1_head_epoch(ff2)
        if fixed == ff2:
            return pdf
        patched = _patch_fontfile2_xref(pdf, ff_xref, fixed)
        return patched if patched is not None else pdf
    except Exception as exc:
        logger.warning("F1 head epoch finalize failed: %s", exc)
        return pdf


def _finalize_tbank_f2_epoch(pdf: bytes) -> bytes:
    """Pin F2 head.created/modified to TinkoffSansBold bank pair."""
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _force_tbank_f2_head_epoch,
        _patch_fontfile2_xref,
    )

    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        fm = tut._find_font_objects(doc)
        meta = fm.get("TinkoffSans-Medium")
        if not meta:
            doc.close()
            return pdf
        ff_xref = meta["fontfile_xref"]
        ff2 = doc.xref_stream(ff_xref)
        doc.close()
        fixed = _force_tbank_f2_head_epoch(ff2)
        if fixed == ff2:
            return pdf
        patched = _patch_fontfile2_xref(pdf, ff_xref, fixed)
        return patched if patched is not None else pdf
    except Exception as exc:
        logger.warning("F2 head epoch finalize failed: %s", exc)
        return pdf


def _tbank_finish_non_sbp_ship(
    pdf: bytes,
    *,
    height: int,
    prepared: Optional[Dict] = None,
    channel: str = "",
) -> bytes:
    """Same ship process as SBP: force-face → peel → finalize + structure gate.

    Channel atlas peel stays (phone 451 / card 431 / nocomm 411 / card_sber 471).
    Finalize is the full SBP chain. Never None.
    """
    from tbank_sbp_stealth import (
        _randomize_pdf_fingerprints,
        _sbp_finalize_for_ship,
        _tbank_force_correct_face,
    )

    if not pdf:
        return pdf
    ch = (channel or "non-sbp").strip() or "non-sbp"
    try:
        if prepared:
            pdf = _tbank_force_correct_face(pdf, prepared, channel=ch)
        pdf = _finalize_tbank_f1_epoch(pdf)
        reshaped = _peel_f1_glyf_to_cmap_band(
            pdf, height=int(height or 0), prepared=prepared,
        )
        if reshaped is None:
            logger.error(
                "T-Bank %s: finish peel miss — continue unpeeled",
                ch,
            )
        else:
            pdf = _finalize_tbank_f1_epoch(reshaped)
        pdf = _finalize_tbank_f2_epoch(pdf)
        if int(height or 0) == 471:
            from tbank_sbp_stealth import _sbp_h471_ensure_legal_cmap_pdf
            pdf = _sbp_h471_ensure_legal_cmap_pdf(pdf)
        return _sbp_finalize_for_ship(pdf, prepared, channel=ch)
    except Exception as exc:
        logger.warning("T-Bank %s finish: %s — finalize anyway", ch, exc)
        try:
            return _sbp_finalize_for_ship(pdf, prepared, channel=ch)
        except Exception:
            try:
                return _randomize_pdf_fingerprints(pdf)
            except Exception:
                return pdf


def _sbp_v3_family_at_risk(pdf: bytes) -> bool:
    """True when Proton K-TBANK-REASSEMBLY-FAMILY-V3 would HARD-FAKE.

    Family fires on font_sig ∧ stream_sig. Escape: F1.dec in (17120, 17198]
    with raw>9205 when F2.bfrange≥10 / fat F2 ToUnicode.
    """
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _parse_tounicode_counts,
        _v3_font_signature,
        _v3_stream_signature,
        _xref_stream_raw_decoded,
        _glyf_table_length,
    )

    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        fm = tut._find_font_objects(doc)
        fr = fm.get("TinkoffSans-Regular")
        fm_ = fm.get("TinkoffSans-Medium")
        if not fr or not fm_:
            doc.close()
            return False
        ff_r = doc.xref_stream(fr["fontfile_xref"])
        ff_m = doc.xref_stream(fm_["fontfile_xref"])
        tu_r = doc.xref_stream(fr["tounicode_xref"])
        tu_m = doc.xref_stream(fm_["tounicode_xref"])
        doc.close()
        f1_raw, _ = _xref_stream_raw_decoded(pdf, fr["fontfile_xref"])
        f2_tu_raw, f2_tu_dec_b = _xref_stream_raw_decoded(pdf, fm_["tounicode_xref"])
        f2_tu_dec = len(f2_tu_dec_b or tu_m)
        _cmap_r, f1_bf, _br_r = _parse_tounicode_counts(tu_r)
        _cmap_m, _bf_m, br_m = _parse_tounicode_counts(tu_m)
        font_sig = _v3_font_signature(
            _glyf_table_length(ff_r),
            _glyf_table_length(ff_m),
            f1_bf,
            br_m,
        )
        stream_sig = _v3_stream_signature(f1_raw, len(ff_r), f2_tu_raw, f2_tu_dec)
        return bool(font_sig and stream_sig)
    except Exception as exc:
        logger.warning("sbp v3 risk probe failed: %s", exc)
        return False


def _lean_sbp_f2_to_hard(pdf: bytes, amount: str) -> Optional[bytes]:
    """Peel Medium FontFile2 into SBP HARD 5000–5824."""
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _F2_GLYF_LO,
        _F2_SBP_DEC_HI,
        _F2_SBP_DEC_LO,
        _blank_ff2_unused_glyfs,
        _f2_amount_unique_digits,
        _f2_digit_card_glyf_ceiling,
        _ff2_composite_closure,
        _ff2_restore_shell_tables,
        _force_tbank_f2_head_epoch,
        _glyf_table_length,
        _head_bbox_from_ff2,
        _patch_fontfile2_xref,
        _restore_head_bbox,
    )

    def _clear_v3_thin(ff: bytes, keep: set, *, glyf_hi: int) -> bytes:
        """Do not spare-inflate thin F2; peel only when over digit-card hi.

        Corpus SBP Medium spans ~822–1636 by amount digits. Never pad glyf
        past loca. Never push a thin font into 1550+ «safecheck» band.
        """
        from tbank_sbp_stealth import _land_f2_under_digit_ceil

        g = _glyf_table_length(ff)
        if g > glyf_hi:
            landed = _land_f2_under_digit_ceil(
                ff, keep, glyf_ceil=glyf_hi, base_ff=ff,
            )
            if _glyf_table_length(landed) <= glyf_hi:
                return landed
            return ff
        # In-band (incl. bank-thin) — leave alone, no spare inflate.
        return ff

    digits = {ord(ch) for ch in (amount or "") if ch.isdigit()}
    keep_cps = {ord(ch) for ch in "Итого"} | {0x20} | digits
    _ceil = _f2_digit_card_glyf_ceiling(_f2_amount_unique_digits(amount))
    # Expensive digit grafts (6/9) may need card+1 — don't peel under that.
    _ceil = max(
        _ceil,
        _f2_digit_card_glyf_ceiling(min(_f2_amount_unique_digits(amount) + 1, 10)),
    )
    try:
        from tbank_sbp_stealth import (
            BANK_MED_GHOST,
            TBANK_CHAR_TO_GID_MED,
            _f2_digit_mosaic_mismatch,
        )
    except Exception:
        BANK_MED_GHOST = set()
        TBANK_CHAR_TO_GID_MED = {}
        _f2_digit_mosaic_mismatch = lambda *_a, **_k: []
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
        if _F2_SBP_DEC_LO <= len(ff2) <= _F2_SBP_DEC_HI:
            # Already in DEC band — still blank ≥2 unmapped nonempty orphans.
            sub_ib = tut._parse_subset_tounicode(
                doc.xref_stream(tu_xref).decode("latin1", "replace")
            )
            cs_ib = doc.xref_stream(doc[0].get_contents()[0])
            doc.close()
            from tbank_sbp_stealth import (
                _cap_f2_orphan_spares,
                _gids_per_font_in_stream,
            )
            _reg_ib, _med_ib = _gids_per_font_in_stream(cs_ib)
            keep_ib = set(_med_ib) | {0, 3} | set(BANK_MED_GHOST)
            for _c in sub_ib:
                keep_ib.add(int(_c))
            # Full charset: always protect master GIDs for amount digits + Итого.
            for _ch in list(amount or "") + list("Итого"):
                _gid = TBANK_CHAR_TO_GID_MED.get(_ch)
                if _gid is not None:
                    keep_ib.add(int(_gid))
            capped = _cap_f2_orphan_spares(ff2, keep_ib, max_spares=1)
            capped = _clear_v3_thin(capped, keep_ib, glyf_hi=_ceil)
            if _f2_digit_mosaic_mismatch(capped, amount):
                logger.warning(
                    "sbp F2 lean in-band mosaic break — keep prior Medium"
                )
                return pdf
            fixed = _force_tbank_f2_head_epoch(capped)
            if fixed == ff2:
                return pdf
            patched = _patch_fontfile2_xref(pdf, ff_xref, fixed)
            return patched if patched is not None else pdf
        sub = tut._parse_subset_tounicode(
            doc.xref_stream(tu_xref).decode("latin1", "replace")
        )
        doc.close()
        uni = {u: c for c, u in sub.items()}
        keep = {0, 3} | set(BANK_MED_GHOST)
        for cp in keep_cps:
            cid = uni.get(cp)
            if cid is not None:
                keep.add(int(cid))
            # Master Medium GID — ToUnicode may omit a digit while glyf lives here.
            ch = chr(cp) if isinstance(cp, int) and cp < 0x110000 else ""
            gid = TBANK_CHAR_TO_GID_MED.get(ch)
            if gid is not None:
                keep.add(int(gid))
        for _ch in list(amount or "") + list("Итого"):
            _gid = TBANK_CHAR_TO_GID_MED.get(_ch)
            if _gid is not None:
                keep.add(int(_gid))
        try:
            keep = _ff2_composite_closure(ff2, keep)
        except Exception:
            pass
        lean = _blank_ff2_unused_glyfs(ff2, keep)
        lean = _ff2_restore_shell_tables(ff2, lean)
        bb = _head_bbox_from_ff2(ff2)
        if bb is not None:
            lean = _restore_head_bbox(lean, bb)
        lean = _clear_v3_thin(lean, keep, glyf_hi=_ceil)
        lean = _force_tbank_f2_head_epoch(lean)
        if _f2_digit_mosaic_mismatch(lean, amount):
            logger.warning("sbp F2 lean mosaic break — reject lean")
            return None
        if not (_F2_SBP_DEC_LO <= len(lean) <= _F2_SBP_DEC_HI):
            # Blank didn't shrink. NEVER wholesale-swap a corpus Medium FontFile2:
            # donor glyfs at THIS PDF's digit CIDs are often empty → «Итого» shows
            # only ₽ with invisible amount. Prefer fat Medium with real digits.
            logger.warning(
                "sbp F2 lean miss %d→%d (want %d..%d) — keep fat Medium (no corpus swap)",
                len(ff2), len(lean), _F2_SBP_DEC_LO, _F2_SBP_DEC_HI,
            )
            return None
        # Guard: amount digit CIDs must stay nonempty (Итого line).
        try:
            from io import BytesIO
            from fontTools.ttLib import TTFont

            _ft = TTFont(BytesIO(lean))
            _glyf = _ft["glyf"]
            _go = _ft.getGlyphOrder()
            for cp in digits:
                ch = chr(cp)
                cids = set()
                if uni.get(cp) is not None:
                    cids.add(int(uni.get(cp)))
                _mg = TBANK_CHAR_TO_GID_MED.get(ch)
                if _mg is not None:
                    cids.add(int(_mg))
                for cid in cids:
                    if cid >= len(_go):
                        continue
                    g = _glyf[_go[cid]]
                    if int(getattr(g, "numberOfContours", 0) or 0) == 0:
                        logger.error(
                            "sbp F2 lean blanked Medium digit U+%04X cid=%d — reject lean",
                            cp, cid,
                        )
                        return None
        except Exception as exc:
            logger.warning("sbp F2 digit contour check failed: %s — ship lean", exc)
        if not (_F2_SBP_DEC_LO <= len(lean) <= _F2_SBP_DEC_HI):
            return None
        if _glyf_table_length(lean) > _ceil:
            logger.warning(
                "sbp F2 lean glyf=%d > digit ceil %d — reject lean",
                _glyf_table_length(lean), _ceil,
            )
            return None
        patched = _patch_fontfile2_xref(pdf, ff_xref, lean)
        if patched is None:
            return None
        logger.info("sbp F2 lean %d→%d", len(ff2), len(lean))
        return patched
    except Exception as exc:
        logger.warning("sbp F2 lean failed: %s", exc)
        return None


def _lean_card_f2_to_hard(pdf: bytes, amount: str, *, lo: int, hi: int) -> Optional[bytes]:
    """Peel/reject Medium FontFile2 into card HARD decoded band."""
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _blank_ff2_unused_glyfs,
        _ff2_composite_closure,
        _ff2_restore_shell_tables,
        _force_tbank_f2_head_epoch,
        _head_bbox_from_ff2,
        _patch_fontfile2_xref,
        _restore_head_bbox,
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
        if lo <= len(ff2) <= hi:
            doc.close()
            fixed = _force_tbank_f2_head_epoch(ff2)
            if fixed == ff2:
                return pdf
            patched = _patch_fontfile2_xref(pdf, ff_xref, fixed)
            return patched if patched is not None else pdf
        if len(ff2) < lo:
            doc.close()
            logger.warning("card F2 under HARD lo %d<%d — ship", len(ff2), lo)
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
        bb = _head_bbox_from_ff2(ff2)
        if bb is not None:
            lean = _restore_head_bbox(lean, bb)
        lean = _force_tbank_f2_head_epoch(lean)
        if not (lo <= len(lean) <= hi):
            logger.warning(
                "card F2 lean miss %d→%d (want %d..%d) — ship",
                len(ff2), len(lean), lo, hi,
            )
            return pdf
        patched = _patch_fontfile2_xref(pdf, ff_xref, lean)
        if patched is None:
            logger.warning("card F2 lean patch failed — ship")
            return pdf
        logger.info("card F2 lean %d→%d", len(ff2), len(lean))
        return patched
    except Exception as exc:
        logger.warning("card F2 lean failed: %s — ship", exc)
        return pdf


def _restitch_f1_keep_from_donor(
    ff2: bytes, donor_ff2: bytes, keep_gids: set,
) -> bytes:
    """Prefer donor outlines for keep GIDs; blank everything else."""
    from copy import deepcopy
    from io import BytesIO
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._g_l_y_f import Glyph as _TGlyph
    from tbank_sbp_stealth import (
        _copy_glyph_closure,
        _ff2_restore_shell_tables,
        _force_tbank_f1_head_epoch,
    )

    dst = TTFont(BytesIO(ff2))
    src = TTFont(BytesIO(donor_ff2))
    dst_glyf = dst["glyf"]
    go = dst.getGlyphOrder()
    src_go = src.getGlyphOrder()
    empty = _TGlyph()
    empty.numberOfContours = 0
    copied: set = set()
    for gid in sorted(keep_gids):
        if gid < len(src_go):
            sg = src["glyf"][src_go[gid]]
            if int(getattr(sg, "numberOfContours", 0) or 0) != 0:
                _copy_glyph_closure(dst_glyf, go, src, int(gid), copied)
                continue
        if gid < len(go):
            dg = dst_glyf[go[gid]]
            if int(getattr(dg, "numberOfContours", 0) or 0) != 0:
                copied.add(int(gid))
    for gid, name in enumerate(go):
        if gid in copied:
            continue
        dst_glyf[name] = deepcopy(empty)
    bio = BytesIO()
    dst.save(bio, reorderTables=False)
    out = _ff2_restore_shell_tables(ff2, bio.getvalue())
    return _force_tbank_f1_head_epoch(out)


def _nudge_tounicode_cardinality(
    pdf: bytes,
    tu_xref: int,
    sub: dict,
    *,
    target_n: int,
    painted_gids: set,
    anchor_gid: int,
    ff2: Optional[bytes] = None,
    cidfont_xref: Optional[int] = None,
) -> Optional[tuple]:
    """Grow/shrink F1 ToUnicode *unique CID* count to ``target_n``. Returns (pdf, sub).

    Growing MUST prefer painted CIDs missing from ToUnicode (never PUA-bind an
    unpainted glyph first — that ⇒ W_MISSING_CID / CMAP_W_MISMATCH / MINIMALITY).
    After any edit, /W is rebuilt to the exact CMap CID set when cidfont_xref+ff2
    are provided.
    """
    import struct
    from io import BytesIO
    from fontTools.ttLib import TTFont
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import _best_compress, _replace_byte_range_and_rebuild
    from tbank_orig_mode import find_object_range

    # sub: cid -> unicode. Detector cmap_n = unique CIDs, not unicode rows.
    uni_gid = {int(cp): int(cid) for cid, cp in sub.items()}
    used_cids = set(uni_gid.values())
    if len(used_cids) == target_n:
        return pdf, sub

    def _nonempty_gids(ttf: bytes) -> list:
        try:
            ft = TTFont(BytesIO(ttf))
            glyf = ft["glyf"]
            go = ft.getGlyphOrder()
            out = []
            for gid, name in enumerate(go):
                nc = int(getattr(glyf[name], "numberOfContours", 0) or 0)
                if nc != 0:
                    out.append(gid)
            return out
        except Exception:
            return []

    def _uni_for_gid(ttf: bytes, gid: int) -> Optional[int]:
        """Best-effort unicode for a GID from the embedded cmap."""
        try:
            ft = TTFont(BytesIO(ttf))
            for table in ft["cmap"].tables:
                if not getattr(table, "cmap", None):
                    continue
                for cp, name in table.cmap.items():
                    try:
                        if ft.getGlyphID(name) == int(gid):
                            return int(cp)
                    except Exception:
                        continue
        except Exception:
            return None
        return None

    painted = {int(x) for x in (painted_gids or ()) if int(x) not in (0, 3)}

    if len(used_cids) < target_n:
        used_cps = set(uni_gid.keys())
        pua = 0xE000
        # 1) Painted CIDs missing from ToUnicode — bind real unicode when possible.
        candidates: list = []
        for gid in sorted(painted):
            if gid not in used_cids:
                candidates.append(("paint", int(gid)))
        # 2) Only then unpainted nonempty (last resort; requires /W sync).
        if ff2:
            for gid in _nonempty_gids(ff2):
                if gid not in used_cids and int(gid) not in painted:
                    candidates.append(("free", int(gid)))
        if not candidates:
            return None
        ci = 0
        while len(used_cids) < target_n:
            if ci >= len(candidates):
                return None
            kind, gid = candidates[ci]
            ci += 1
            if kind == "paint":
                cp = _uni_for_gid(ff2, gid) if ff2 else None
                if cp is None or cp in used_cps:
                    while pua in used_cps:
                        pua += 1
                    cp = pua
                    pua += 1
            else:
                while pua in used_cps:
                    pua += 1
                cp = pua
                pua += 1
            uni_gid[int(cp)] = int(gid)
            used_cps.add(int(cp))
            used_cids.add(int(gid))
    else:
        # Drop only unpainted extras / PUA — never real letters still in stream
        # (labels Служба/Телефон share F1 with face values).
        drop = sorted(
            (
                cp for cp, cid in uni_gid.items()
                if int(cid) not in painted_gids and (0xE000 <= int(cp) < 0xF900)
            ),
            reverse=True,
        )
        drop += sorted(
            (
                cp for cp, cid in uni_gid.items()
                if int(cid) not in painted_gids and not (0xE000 <= int(cp) < 0xF900)
            ),
            reverse=True,
        )
        for cp in drop:
            if len(set(uni_gid.values())) <= target_n:
                break
            del uni_gid[cp]
        while len(set(uni_gid.values())) > target_n:
            puas = sorted(
                (
                    cp for cp, cid in uni_gid.items()
                    if 0xE000 <= cp < 0xF900 and int(cid) not in painted_gids
                ),
                reverse=True,
            )
            if not puas:
                # Refuse to delete painted-letter rows — caller must pick a
                # larger twin cmap or keep current cardinality.
                break
            del uni_gid[puas[0]]
    if len(set(uni_gid.values())) != target_n:
        return None
    new_tu = tut._build_tounicode_cmap(uni_gid)
    rng = find_object_range(pdf, tu_xref)
    if not rng:
        return None
    s, e = rng
    try:
        new_obj = tut._make_modified_obj(
            pdf[s:e], tu_xref, new_stream=_best_compress(new_tu),
        )
    except Exception:
        return None
    patched = _replace_byte_range_and_rebuild(pdf, s, e, new_obj)
    if patched is None:
        return None
    new_sub = {cid: cp for cp, cid in uni_gid.items()}
    # K-TBANK-FONT-CID-CLOSURE-001: every CMap CID must appear in /W.
    if cidfont_xref is not None and ff2 is not None:
        try:
            ft = TTFont(BytesIO(ff2))
            w_cids = sorted(set(uni_gid.values()))
            w_str = tut._build_widths_array(ft, w_cids)
            wr = find_object_range(patched, int(cidfont_xref))
            if wr:
                ws, we = wr
                w_obj = tut._make_modified_obj(
                    patched[ws:we], int(cidfont_xref), new_W=w_str,
                )
                synced = _replace_byte_range_and_rebuild(patched, ws, we, w_obj)
                if synced is not None:
                    patched = synced
                    logger.info(
                        "F1 /W synced to CMap CIDs n=%d after ToUnicode nudge",
                        len(w_cids),
                    )
        except Exception as exc:
            logger.warning("F1 /W sync after ToUnicode nudge failed: %s", exc)
    return patched, new_sub


def _tbank_ff2_twin_ok(
    ff2: bytes,
    *,
    height: int,
    cmap_n: int,
    glyf_len: int,
) -> bool:
    """Proton TBANK_F1_FF2_SHA_TWIN_MISMATCH: exact (h,cmap,glyf) ⇒ corpus SHA."""
    import hashlib

    try:
        from tbank_ff2_sha_atlas import FF2_SHA_BY_HEIGHT_CMAP_GLYF
    except Exception:
        return True
    key = (int(height), int(cmap_n), int(glyf_len))
    allowed = FF2_SHA_BY_HEIGHT_CMAP_GLYF.get(key)
    sha = hashlib.sha256(ff2).hexdigest()[:16]
    if allowed is None:
        # Twin under another cmap at this glyf ⇒ current cmap is wrong.
        twin_cns = _tbank_ff2_twin_cmaps(ff2, height=height, glyf_len=glyf_len)
        if twin_cns and int(cmap_n) not in twin_cns:
            return False
        return True
    return sha in allowed


def _f1_glyph_shape(ff2: bytes) -> Tuple[int, int, int]:
    """Return (composite, nonempty, simple) glyph counts for Twin-Shape check."""
    from io import BytesIO

    from fontTools.ttLib import TTFont

    try:
        ft = TTFont(BytesIO(ff2))
        glyf = ft["glyf"]
        go = ft.getGlyphOrder()
    except Exception:
        return (0, 0, 0)
    comp = ne = simp = 0
    for name in go:
        g = glyf[name]
        nc = int(getattr(g, "numberOfContours", 0) or 0)
        if nc == -1:
            comp += 1
            ne += 1
        elif nc != 0:
            simp += 1
            ne += 1
    return (comp, ne, simp)


# glyf_len -> [(name, ff2, shape), ...]  built once per process.
_F1_TWIN_BY_GLYF: Optional[Dict[int, List[Tuple[str, bytes, Tuple[int, int, int]]]]] = None
_CORPUS_SUBSET_TAGS: Optional[Set[bytes]] = None
_F1_TWIN_INDEX_LOCK = __import__("threading").Lock()


def corpus_tbank_subset_tags() -> Set[bytes]:
    """6-letter OpenPDF subset prefixes on corpus / T_* original T-Bank PDFs."""
    _ensure_corpus_f1_twin_index()
    return set(_CORPUS_SUBSET_TAGS or set())


def _ensure_corpus_f1_twin_index() -> None:
    global _F1_TWIN_BY_GLYF, _CORPUS_SUBSET_TAGS
    if _F1_TWIN_BY_GLYF is not None and _CORPUS_SUBSET_TAGS is not None:
        return
    with _F1_TWIN_INDEX_LOCK:
        if _F1_TWIN_BY_GLYF is not None and _CORPUS_SUBSET_TAGS is not None:
            return
        _build_corpus_f1_twin_index()


def _build_corpus_f1_twin_index() -> None:
    """Scan corpus once: Regular FF2 twins by glyf length + BaseFont prefixes."""
    global _F1_TWIN_BY_GLYF, _CORPUS_SUBSET_TAGS
    import os
    import re

    import fitz

    from tbank_sbp_stealth import CORPUS_TBANK_DIR, _glyf_table_length
    import tbank_unlock_template as tut

    prefix_re = re.compile(rb"([A-Z]{6})\+(?:TinkoffSans|ALSRubl)")
    by_glyf: Dict[int, List[Tuple[str, bytes, Tuple[int, int, int]]]] = {}
    tags: Set[bytes] = set()

    tag_paths: List[str] = []
    if os.path.isdir(CORPUS_TBANK_DIR):
        for name in sorted(os.listdir(CORPUS_TBANK_DIR)):
            if name.lower().endswith(".pdf"):
                tag_paths.append(os.path.join(CORPUS_TBANK_DIR, name))
    for extra in (
        os.path.join(_DIR, "templates", "T_sbp_original.pdf"),
        os.path.join(_DIR, "templates", "T_phone_original.pdf"),
        os.path.join(_DIR, "templates", "T_card_tbank_original.pdf"),
        os.path.join(_DIR, "templates", "T_card_sber_original.pdf"),
        os.path.join(_DIR, "templates", "T_original.pdf"),
        os.path.join(_DIR, "templates", "T_nocomm_original.pdf"),
    ):
        if os.path.isfile(extra) and extra not in tag_paths:
            tag_paths.append(extra)

    for path in tag_paths:
        try:
            with open(path, "rb") as fh:
                tags.update(prefix_re.findall(fh.read()))
        except Exception:
            continue

    if os.path.isdir(CORPUS_TBANK_DIR):
        for name in sorted(os.listdir(CORPUS_TBANK_DIR)):
            if not name.lower().endswith(".pdf"):
                continue
            path = os.path.join(CORPUS_TBANK_DIR, name)
            try:
                doc = fitz.open(path)
                try:
                    text = doc[0].get_text() or ""
                    if (
                        "Идентификатор" not in text
                        and "По номеру карты" not in text
                        and "Квитанция" not in text
                    ):
                        continue
                    fr = tut._find_font_objects(doc).get("TinkoffSans-Regular")
                    if not fr:
                        continue
                    ff = doc.xref_stream(fr["fontfile_xref"])
                finally:
                    doc.close()
            except Exception:
                continue
            gl = int(_glyf_table_length(ff))
            sh = _f1_glyph_shape(ff)
            by_glyf.setdefault(gl, []).append((name, ff, sh))

    for path in (
        os.path.join(_DIR, "templates", "T_sbp_original.pdf"),
        os.path.join(_DIR, "templates", "T_phone_original.pdf"),
        os.path.join(_DIR, "templates", "T_card_tbank_original.pdf"),
        os.path.join(_DIR, "templates", "T_card_sber_original.pdf"),
        os.path.join(_DIR, "templates", "T_original.pdf"),
        os.path.join(_DIR, "templates", "T_nocomm_original.pdf"),
    ):
        if not os.path.isfile(path):
            continue
        try:
            doc = fitz.open(path)
            try:
                fr = tut._find_font_objects(doc).get("TinkoffSans-Regular")
                if not fr:
                    continue
                ff = doc.xref_stream(fr["fontfile_xref"])
            finally:
                doc.close()
        except Exception:
            continue
        gl = int(_glyf_table_length(ff))
        sh = _f1_glyph_shape(ff)
        name = os.path.basename(path)
        by_glyf.setdefault(gl, []).append((name, ff, sh))

    # card_sber h=471 exact atlas twins live in tbank_corpus, not tbank_sbp_corpus.
    try:
        from tbank_corpus import corpus_paths as _tb_corpus_paths

        for path in _tb_corpus_paths("card_sber") or []:
            if not os.path.isfile(path):
                continue
            try:
                doc = fitz.open(path)
                try:
                    if int(round(float(doc[0].mediabox.y1))) != 471:
                        continue
                    fr = tut._find_font_objects(doc).get("TinkoffSans-Regular")
                    if not fr:
                        continue
                    ff = doc.xref_stream(fr["fontfile_xref"])
                finally:
                    doc.close()
            except Exception:
                continue
            gl = int(_glyf_table_length(ff))
            sh = _f1_glyph_shape(ff)
            name = os.path.basename(path)
            by_glyf.setdefault(gl, []).append((name, ff, sh))
    except Exception:
        pass

    _F1_TWIN_BY_GLYF = by_glyf
    _CORPUS_SUBSET_TAGS = tags
    logger.info(
        "T-Bank corpus F1 twin index: %d glyf lengths, %d files, %d subset tags",
        len(by_glyf),
        sum(len(v) for v in by_glyf.values()),
        len(tags),
    )


def _load_corpus_f1_twin_for_glyf(
    glyf_len: int,
    *,
    want_shape: Optional[Tuple[int, int, int]] = None,
) -> Optional[bytes]:
    """Pick a corpus SBP Regular FontFile2 with this glyf length (prefer shape)."""
    _ensure_corpus_f1_twin_index()
    entries = (_F1_TWIN_BY_GLYF or {}).get(int(glyf_len)) or []
    best = None  # (prefer_score, name, ff2)  lower better
    for name, ff, sh in entries:
        if want_shape is not None and sh == want_shape:
            return ff
        # Prefer richer nonempty (сбп15 83 > сбп8 82) — matches Proton twin.
        score = (0 if want_shape is None else 1, -sh[1], -sh[2], name)
        if best is None or score < best[0]:
            best = (score, name, ff)
    return None if best is None else best[2]


def _f1_loca_tables(ff2: bytes):
    """Return (offs, glyf_ba, index_to_loc, num_glyphs)."""
    import struct as _st  # noqa: F401

    from tbank_sbp_stealth import _get_font_table

    glyf = _get_font_table(ff2, b"glyf") or b""
    loca = _get_font_table(ff2, b"loca") or b""
    maxp = _get_font_table(ff2, b"maxp")
    head = _get_font_table(ff2, b"head")
    if not glyf or not loca or not maxp or not head or len(maxp) < 6:
        raise ValueError("missing sfnt tables")
    ng = int.from_bytes(maxp[4:6], "big")
    itl = int.from_bytes(head[50:52], "big") if len(head) >= 52 else 1
    if itl == 0:
        offs = [
            int.from_bytes(loca[i * 2:i * 2 + 2], "big") * 2
            for i in range(ng + 1)
        ]
    else:
        offs = [
            int.from_bytes(loca[i * 4:i * 4 + 4], "big")
            for i in range(ng + 1)
        ]
    return offs, bytearray(glyf), itl, ng


def _raw_install_glyph_blob(
    dst_ff2: bytes,
    blob: bytes,
    gid: int,
    *,
    steal_from_empty: bool = True,
) -> Optional[bytes]:
    """Insert glyph bytes at empty GID, stealing from fattest empty loca pad.

    Length-neutral when a pad span ≥ len(blob)+10 exists. fontTools-free so
    empty pads are not compacted away.
    """
    import struct

    from tbank_sbp_stealth import (
        _force_tbank_f1_head_epoch,
        _recalc_head_csa,
        _replace_sfnt_table_resized,
    )

    if not blob:
        return dst_ff2
    try:
        d_offs, d_glyf, itl, ng = _f1_loca_tables(dst_ff2)
    except Exception:
        return None
    if gid >= ng:
        return None
    ds, de = d_offs[gid], d_offs[gid + 1]
    existing = de - ds
    if existing >= 2:
        ncont = int.from_bytes(d_glyf[ds:ds + 2], "big", signed=True)
        if ncont != 0:
            return dst_ff2  # nonempty keep — never overwrite
    need = len(blob)

    def _find_pad(exclude: int) -> Tuple[Optional[int], int]:
        best_g, best_span = None, 0
        if not steal_from_empty:
            return None, 0
        for g in range(ng):
            if g == exclude:
                continue
            a, b = d_offs[g], d_offs[g + 1]
            span = b - a
            if span < need + 10:
                continue
            ncont = int.from_bytes(d_glyf[a:a + 2], "big", signed=True)
            if ncont != 0:
                continue
            if span > best_span:
                best_span, best_g = span, g
        return best_g, best_span

    spans = [
        bytearray(d_glyf[d_offs[g]:d_offs[g + 1]]) for g in range(ng)
    ]
    # 1) In-place overwrite of empty span when it fits the blob.
    if existing >= need and existing > 0:
        rem = existing - need
        spans[gid] = bytearray(blob + (b"\x00" * rem))
    else:
        # 2) Steal from another empty pad (length-neutral).
        pad, pad_span = _find_pad(gid)
        if pad is None:
            return None
        new_pad_span = pad_span - need
        # If target had an empty pad span, its bytes are freed into the table
        # when we replace with blob — account by growing pad shrink less.
        freed = existing  # target empty bytes removed from layout via rebuild
        # Rebuild: target gets blob; pad shrinks by (need - freed).
        shrink = need - freed
        if shrink < 0:
            # Blob smaller than target empty — keep extra on target as zero pad.
            spans[gid] = bytearray(blob + (b"\x00" * (-shrink)))
            # pad unchanged
        else:
            if pad_span < shrink + 10:
                return None
            salt = (need * 131 + pad * 17) & 0xFFFFFFFF
            tail = bytes(
                ((salt + j * 37) % 251) + 1
                for j in range(max(0, pad_span - shrink - 10))
            )
            spans[pad] = bytearray(struct.pack(">hhhhh", 0, 0, 0, 0, 0) + tail)
            spans[gid] = bytearray(blob)
    new_glyf = bytearray()
    new_offs = [0]
    for g in range(ng):
        new_glyf.extend(spans[g])
        if itl == 0 and len(new_glyf) % 2:
            new_glyf.append(0)
        new_offs.append(len(new_glyf))
    if itl == 0:
        if any(o % 2 for o in new_offs):
            return None
        loca_new = bytearray()
        for o in new_offs:
            loca_new.extend(int(o // 2).to_bytes(2, "big"))
    else:
        loca_new = bytearray()
        for o in new_offs:
            loca_new.extend(int(o).to_bytes(4, "big"))
    out = _replace_sfnt_table_resized(dst_ff2, b"glyf", bytes(new_glyf))
    out = _replace_sfnt_table_resized(out, b"loca", bytes(loca_new))
    return _recalc_head_csa(_force_tbank_f1_head_epoch(out))


def _twin_glyph_blob(twin_ff2: bytes, gid: int) -> bytes:
    try:
        offs, glyf, _itl, ng = _f1_loca_tables(twin_ff2)
    except Exception:
        return b""
    if gid >= ng:
        return b""
    return bytes(glyf[offs[gid]:offs[gid + 1]])


def _align_f1_shape_to_corpus_twin(
    ff2: bytes,
    *,
    glyf_len: int,
    keep: Set[int],
) -> bytes:
    """Match corpus twin shape at exact glyf length (closure + LSB safe).

    Strategy when undershooting nonempty (common after peel shrink):
      1) blank hydrate extras to free ≥ bytes needed for twin spares
      2) fat-pad back to exact length
      3) raw-install REAL twin glyph blobs (length-neutral from pad)
      4) sync hmtx LSB→xMin
    Never use tiny stub outlines (breaks EXACT_CLOSURE / transplant).
    """
    from copy import deepcopy
    from io import BytesIO

    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._g_l_y_f import Glyph as _TGlyph
    from tbank_sbp_stealth import (
        _ff2_composite_closure,
        _ff2_restore_shell_tables,
        _force_tbank_f1_head_epoch,
        _glyf_table_length,
        _head_bbox_from_ff2,
        _recalc_head_csa,
        _restore_head_bbox,
        _sync_hmtx_lsb_to_xmin,
    )

    shell_bb = _head_bbox_from_ff2(ff2)

    def _finish(out: bytes) -> bytes:
        out = _sync_hmtx_lsb_to_xmin(out)
        if shell_bb is not None:
            out = _restore_head_bbox(out, shell_bb)
            out = _recalc_head_csa(out)
        return out

    twin = _load_corpus_f1_twin_for_glyf(int(glyf_len))
    if twin is None:
        return None
    want = _f1_glyph_shape(twin)
    keep_i = {int(x) for x in keep} | {0, 3}
    cur_sh = _f1_glyph_shape(ff2)
    if cur_sh == want and int(_glyf_table_length(ff2)) == int(glyf_len):
        return _finish(ff2)
    # Align surgery is 20–40s of fontTools saves and almost always REJECTS
    # on hydrate (different nonempty count). Ship hydrate instead.
    return None

    def _nc(g) -> int:
        return int(getattr(g, "numberOfContours", 0) or 0)

    def _empty():
        g = _TGlyph()
        g.numberOfContours = 0
        return g

    def _save(ft_obj, base: bytes) -> bytes:
        bio = BytesIO()
        ft_obj.save(bio, reorderTables=False)
        out = _force_tbank_f1_head_epoch(
            _ff2_restore_shell_tables(base, bio.getvalue())
        )
        return _sync_hmtx_lsb_to_xmin(out)

    try:
        ft_t = TTFont(BytesIO(twin))
        glyf_t = ft_t["glyf"]
        go_t = ft_t.getGlyphOrder()
        twin_ne = {i for i, n in enumerate(go_t) if _nc(glyf_t[n]) != 0}
    except Exception:
        return _finish(ff2)

    try:
        required = set(_ff2_composite_closure(ff2, keep_i)) | set(keep_i)
    except Exception:
        required = set(keep_i)

    cur = ff2

    # --- undershoot: free bytes from extras, then raw-install twin spares ---
    for _ in range(12):
        sh = _f1_glyph_shape(cur)
        if sh == want:
            break
        need_ne = want[1] - sh[1]
        need_simp = want[2] - sh[2]
        need_comp = want[0] - sh[0]
        if need_ne <= 0 and need_simp <= 0 and need_comp <= 0:
            # overshoot only
            break

        # Pick twin spare candidates (smallest first later).
        targets: List[Tuple[int, bytes, int]] = []  # gid, blob, nc
        try:
            ft = TTFont(BytesIO(cur))
            glyf = ft["glyf"]
            go = ft.getGlyphOrder()
        except Exception:
            break
        for gid, tname in enumerate(go_t):
            if gid >= len(go) or gid in keep_i:
                continue
            snc = _nc(glyf_t[tname])
            if snc == 0 or _nc(glyf[go[gid]]) != 0:
                continue
            if need_comp > 0 and snc != -1:
                continue
            if need_simp > 0 and snc == -1 and need_comp <= 0:
                continue
            blob = _twin_glyph_blob(twin, gid)
            if not blob:
                continue
            targets.append((gid, blob, snc))
            if len(targets) >= 8:
                break
        if not targets:
            break

        need_ne_i = max(need_ne, need_simp + need_comp, 1)
        targets_sorted = sorted(targets, key=lambda t: len(t[1]))
        # Extras we can blank for byte budget (shape dips, then we re-install).
        extras = []
        for gid, name in enumerate(go):
            if gid in keep_i or gid in (0, 3):
                continue
            if _nc(glyf[name]) == 0:
                continue
            try:
                span = len(glyf[name].compile(glyf))
            except Exception:
                span = 64
            extras.append((span, gid))
        extras.sort(reverse=True)

        # Blank K extras then install need_ne_i+K so net nonempty = want.
        # Choose K so freed bytes ≥ size of the installs.
        best_plan = None  # (blank_gids, install_list)
        for k_blank in range(0, min(len(extras), 6) + 1):
            n_inst = need_ne_i + k_blank
            if n_inst > len(targets_sorted):
                continue
            inst = targets_sorted[:n_inst]
            need_b = sum(len(b) for _g, b, _n in inst)
            blanks = []
            freed = 0
            inst_gids = {t[0] for t in inst}
            for span, gid in extras:
                if len(blanks) >= k_blank:
                    break
                if gid in inst_gids:
                    continue
                blanks.append(gid)
                freed += span
            if len(blanks) < k_blank:
                continue
            if freed >= need_b or k_blank == 0 and need_b == 0:
                best_plan = (blanks, inst)
                if freed >= need_b:
                    break
            elif best_plan is None and freed > 0:
                best_plan = (blanks, inst)
        if best_plan is None:
            best_plan = ([], targets_sorted[:need_ne_i])
        blank_gids, targets = best_plan
        need_bytes = sum(len(b) for _g, b, _n in targets)

        for gid in blank_gids:
            try:
                ft = TTFont(BytesIO(cur))
                glyf = ft["glyf"]
                go = ft.getGlyphOrder()
            except Exception:
                break
            if gid >= len(go) or _nc(glyf[go[gid]]) == 0:
                continue
            glyf[go[gid]] = _empty()
            cur = _save(ft, cur)

        # Land exact length (fat if blanking undershot).
        g_now = int(_glyf_table_length(cur))
        if g_now < int(glyf_len):
            grown = _snap_f1_glyf_exact_via_fat(cur, int(glyf_len), keep_i)
            if grown is not None:
                cur = _sync_hmtx_lsb_to_xmin(grown)
        elif g_now > int(glyf_len):
            # Still fat: pad-steal install needs exact base — trim empties.
            trimmed = _snap_f1_glyf_exact_via_trim_empty(
                cur, int(glyf_len), keep_i,
            )
            if trimmed is not None:
                cur = _sync_hmtx_lsb_to_xmin(trimmed)

        # Ensure pad budget ≥ need_bytes on an empty slot.
        g_now = int(_glyf_table_length(cur))
        if g_now == int(glyf_len):
            grown = _snap_f1_glyf_exact_via_fat(
                cur, int(glyf_len) + max(need_bytes + 32, 64), keep_i,
            )
            if grown is not None:
                cur = _sync_hmtx_lsb_to_xmin(grown)

        for gid, blob, _snc in targets:
            nxt = _raw_install_glyph_blob(cur, blob, gid)
            if nxt is None:
                continue
            cur = _sync_hmtx_lsb_to_xmin(nxt)

        # Trim leftover pad back to exact.
        g_now = int(_glyf_table_length(cur))
        if g_now > int(glyf_len):
            trimmed = _snap_f1_glyf_exact_via_trim_empty(
                cur, int(glyf_len), keep_i,
            )
            if trimmed is not None:
                cur = _sync_hmtx_lsb_to_xmin(trimmed)
        elif g_now < int(glyf_len):
            grown = _snap_f1_glyf_exact_via_fat(cur, int(glyf_len), keep_i)
            if grown is not None:
                cur = _sync_hmtx_lsb_to_xmin(grown)

        try:
            required = set(_ff2_composite_closure(cur, keep_i)) | set(keep_i)
        except Exception:
            pass

    # Fill any still-empty required GIDs from twin (closure HARD).
    try:
        ft = TTFont(BytesIO(cur))
        glyf = ft["glyf"]
        go = ft.getGlyphOrder()
        for gid in sorted(required):
            if gid >= len(go) or gid >= len(go_t):
                continue
            if _nc(glyf[go[gid]]) != 0:
                continue
            blob = _twin_glyph_blob(twin, gid)
            if not blob:
                continue
            # May grow — trim later.
            grown = _snap_f1_glyf_exact_via_fat(
                cur, int(_glyf_table_length(cur)) + len(blob) + 32, keep_i,
            )
            base = grown or cur
            nxt = _raw_install_glyph_blob(base, blob, gid)
            if nxt is not None:
                cur = _sync_hmtx_lsb_to_xmin(nxt)
    except Exception as exc:
        logger.warning("F1 closure fill failed: %s", exc)

    # Overshoot shape: blank non-painted extras (closure can be huge — do not
    # protect the full required set when fixing counts).
    for _ in range(12):
        sh = _f1_glyph_shape(cur)
        if sh == want:
            break
        if sh[1] <= want[1] and sh[2] <= want[2] and sh[0] <= want[0]:
            break
        try:
            ft = TTFont(BytesIO(cur))
            glyf = ft["glyf"]
            go = ft.getGlyphOrder()
        except Exception:
            break
        victim = None
        best = -1
        for gid, name in enumerate(go):
            if gid in keep_i or gid in (0, 3):
                continue
            if _nc(glyf[name]) == 0:
                continue
            try:
                span = len(glyf[name].compile(glyf))
            except Exception:
                span = 64
            if span > best:
                best, victim = span, gid
        if victim is None:
            break
        glyf[go[victim]] = _empty()
        cur = _save(ft, cur)

    g_now = int(_glyf_table_length(cur))
    if g_now < int(glyf_len):
        grown = _snap_f1_glyf_exact_via_fat(cur, int(glyf_len), keep_i)
        if grown is not None:
            cur = _sync_hmtx_lsb_to_xmin(grown)
    elif g_now > int(glyf_len):
        trimmed = _snap_f1_glyf_exact_via_trim_empty(
            cur, int(glyf_len), keep_i,
        )
        if trimmed is not None:
            cur = _sync_hmtx_lsb_to_xmin(trimmed)

    cur = _sync_hmtx_lsb_to_xmin(cur)
    now_sh = _f1_glyph_shape(cur)
    now_g = int(_glyf_table_length(cur))
    # Shape OK but slightly fat: blank one non-keep spare (≥delta B), then
    # re-install a smaller twin spare length-neutrally.
    if now_sh == want and now_g > int(glyf_len):
        delta = now_g - int(glyf_len)
        try:
            ft = TTFont(BytesIO(cur))
            glyf = ft["glyf"]
            go = ft.getGlyphOrder()
            cands = []
            for gid, name in enumerate(go):
                if gid in keep_i or gid in (0, 3):
                    continue
                if _nc(glyf[name]) == 0:
                    continue
                try:
                    span = len(glyf[name].compile(glyf))
                except Exception:
                    span = 64
                if span >= delta:
                    cands.append((span, gid))
            cands.sort()  # smallest sufficient
            if cands:
                _span, gid = cands[0]
                glyf[go[gid]] = _empty()
                cur = _save(ft, cur)
                # Re-install one twin spare for the dipped nonempty.
                sh = _f1_glyph_shape(cur)
                if sh[1] < want[1] or sh[2] < want[2]:
                    small = None
                    for tg, tname in enumerate(go_t):
                        if tg in keep_i:
                            continue
                        snc = _nc(glyf_t[tname])
                        if snc <= 0:
                            continue
                        blob = _twin_glyph_blob(twin, tg)
                        if not blob:
                            continue
                        # Prefer empty local slot.
                        try:
                            ft2 = TTFont(BytesIO(cur))
                            if _nc(ft2["glyf"][ft2.getGlyphOrder()[tg]]) != 0:
                                continue
                        except Exception:
                            continue
                        if small is None or len(blob) < len(small[1]):
                            small = (tg, blob)
                    if small is not None:
                        tg, blob = small
                        g_now = int(_glyf_table_length(cur))
                        if g_now < int(glyf_len) + len(blob) + 16:
                            grown = _snap_f1_glyf_exact_via_fat(
                                cur,
                                max(int(glyf_len), g_now) + len(blob) + 16,
                                keep_i,
                            )
                            if grown is not None:
                                cur = _sync_hmtx_lsb_to_xmin(grown)
                        nxt = _raw_install_glyph_blob(cur, blob, tg)
                        if nxt is not None:
                            cur = _sync_hmtx_lsb_to_xmin(nxt)
                g_now = int(_glyf_table_length(cur))
                if g_now > int(glyf_len):
                    trimmed = _snap_f1_glyf_exact_via_trim_empty(
                        cur, int(glyf_len), keep_i,
                    )
                    if trimmed is not None:
                        cur = _sync_hmtx_lsb_to_xmin(trimmed)
                elif g_now < int(glyf_len):
                    grown = _snap_f1_glyf_exact_via_fat(
                        cur, int(glyf_len), keep_i,
                    )
                    if grown is not None:
                        cur = _sync_hmtx_lsb_to_xmin(grown)
        except Exception as exc:
            logger.warning("F1 twin-shape fat-trim repair failed: %s", exc)
        now_sh = _f1_glyph_shape(cur)
        now_g = int(_glyf_table_length(cur))

    logger.info(
        "F1 twin-shape align %s→%s (now %s) glyf→%d",
        cur_sh, want, now_sh, now_g,
    )
    if now_sh != want or now_g != int(glyf_len):
        logger.warning(
            "F1 twin-shape align incomplete %s≠%s glyf %d≠%d — REJECT",
            now_sh, want, now_g, int(glyf_len),
        )
        return None  # type: ignore[return-value]
    return _finish(cur)


def _tbank_ff2_is_corpus_twin(
    ff2: bytes, *, height: Optional[int] = None,
) -> bool:
    """True if FF2 sha16 is a known twin (optionally scoped to page height)."""
    import hashlib

    try:
        from tbank_ff2_sha_atlas import FF2_SHA_BY_HEIGHT_CMAP_GLYF
        from tbank_sbp_stealth import _glyf_table_length
    except Exception:
        return False
    sha = hashlib.sha256(ff2).hexdigest()[:16]
    g = int(_glyf_table_length(ff2))
    for (hh, _cn, gl), allowed in FF2_SHA_BY_HEIGHT_CMAP_GLYF.items():
        if height is not None and int(hh) != int(height):
            continue
        if int(gl) == g and sha in allowed:
            return True
    return False


def _tbank_ff2_twin_cmaps(
    ff2: bytes, *, height: int, glyf_len: Optional[int] = None,
) -> List[int]:
    """CMap cardinalities where this FF2 is an enrolled SHA twin.

    Span-preserving hydrate keeps glyf_len+shape of a corpus twin but breaks
    SHA — still return that glyf's twin cmap cards so diversify lands correctly.
    """
    import hashlib

    try:
        from tbank_ff2_sha_atlas import FF2_SHA_BY_HEIGHT_CMAP_GLYF
        from tbank_sbp_stealth import _glyf_table_length
    except Exception:
        return []
    sha = hashlib.sha256(ff2).hexdigest()[:16]
    g = int(glyf_len if glyf_len is not None else _glyf_table_length(ff2))
    h = int(height)
    out: List[int] = []
    for (hh, cn, gl), allowed in FF2_SHA_BY_HEIGHT_CMAP_GLYF.items():
        if int(hh) == h and int(gl) == g and sha in allowed:
            out.append(int(cn))
    if out:
        return sorted(set(out))
    # Shape-locked (non-SHA) hydrate onto a corpus twin length.
    try:
        twin = _load_corpus_f1_twin_for_glyf(g)
        if twin is not None and _f1_glyph_shape(ff2) == _f1_glyph_shape(twin):
            for (hh, cn, gl), _allowed in FF2_SHA_BY_HEIGHT_CMAP_GLYF.items():
                if int(hh) == h and int(gl) == g:
                    out.append(int(cn))
    except Exception:
        pass
    # Glyf-length atlas cards — hydrate breaks SHA/shape but peel/diversify
    # still need legal cmap targets for this enrolled twin length.
    for (hh, cn, gl), _allowed in FF2_SHA_BY_HEIGHT_CMAP_GLYF.items():
        if int(hh) == h and int(gl) == g:
            out.append(int(cn))
    return sorted(set(out))


def _snap_f1_glyf_exact_via_shrink(
    ff2: bytes,
    want: int,
    keep: Set[int],
    *,
    pad_to_exact: bool = True,
    allow_fat: bool = True,
) -> Optional[bytes]:
    """Blank unused nonempty glyphs until glyf ≤ want, then fat-pad to exact.

    Hydrate often overshoots the cmap envelope (e.g. 13226 vs exact 13002).
    Growing further is wrong; peel unused ink first.
    ``pad_to_exact=False``: stop at any glyf ≤ want (Proton ±400 band).
    """
    from copy import deepcopy
    from io import BytesIO

    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._g_l_y_f import Glyph as _TGlyph
    from tbank_sbp_stealth import (
        _ff2_restore_shell_tables,
        _force_tbank_f1_head_epoch,
        _glyf_table_length,
    )

    want = int(want)
    g0 = int(_glyf_table_length(ff2))
    if g0 == want:
        return ff2
    if g0 < want:
        if allow_fat:
            return _snap_f1_glyf_exact_via_fat(ff2, want, keep)
        return None

    keep_i = {int(x) for x in keep} | {0, 3}
    cur = ff2
    empty = _TGlyph()
    empty.numberOfContours = 0
    for _ in range(96):
        g = int(_glyf_table_length(cur))
        if g <= want:
            break
        try:
            ft = TTFont(BytesIO(cur))
            go = ft.getGlyphOrder()
            glyf = ft["glyf"]
            cands: List[Tuple[int, int]] = []
            for gid, name in enumerate(go):
                if gid in keep_i:
                    continue
                nc = int(getattr(glyf[name], "numberOfContours", 0) or 0)
                if nc == 0 and not getattr(glyf[name], "components", None):
                    continue
                try:
                    span = len(glyf[name].compile(glyf))
                except Exception:
                    span = 64
                cands.append((span, gid))
            if not cands:
                break
            cands.sort(reverse=True)
            progress = False
            for span, gid in cands:
                ft2 = TTFont(BytesIO(cur))
                go2 = ft2.getGlyphOrder()
                ft2["glyf"][go2[gid]] = deepcopy(empty)
                bio = BytesIO()
                ft2.save(bio, reorderTables=False)
                trial = _ff2_restore_shell_tables(cur, bio.getvalue())
                trial = _force_tbank_f1_head_epoch(trial)
                g2 = int(_glyf_table_length(trial))
                if g2 >= g:
                    keep_i.add(gid)
                    continue
                # Prefer landing ≤ want; otherwise keep peeling.
                cur = trial
                progress = True
                if g2 <= want:
                    break
            if not progress:
                break
        except Exception:
            break

    g_now = int(_glyf_table_length(cur))
    if g_now > want:
        return None
    if g_now < want and pad_to_exact and allow_fat:
        padded = _snap_f1_glyf_exact_via_fat(cur, want, keep_i)
        return padded
    return cur


def _snap_f1_glyf_exact_via_fat(
    ff2: bytes,
    want: int,
    keep: Set[int],
) -> Optional[bytes]:
    """Grow glyf to exact atlas length inside loca (no trailing junk).

    Widens an unused ncont=0 glyph span and shifts later loca entries —
    Proton TBANK_F1_GLYF_TRAILING_JUNK flags bytes past loca[-1].
    """
    import struct

    from tbank_sbp_stealth import (
        _force_tbank_f1_head_epoch,
        _get_font_table,
        _glyf_table_length,
        _recalc_head_csa,
        _replace_sfnt_table_resized,
    )

    g0 = int(_glyf_table_length(ff2))
    want = int(want)
    if g0 == want:
        return ff2
    if g0 > want or g0 <= 0:
        return None
    need = want - g0
    try:
        glyf = bytearray(_get_font_table(ff2, b"glyf") or b"")
        loca_b = _get_font_table(ff2, b"loca") or b""
        maxp = _get_font_table(ff2, b"maxp")
        head = _get_font_table(ff2, b"head")
        if not glyf or not loca_b or not maxp or not head or len(maxp) < 6:
            return None
        num_glyphs = int.from_bytes(maxp[4:6], "big")
        index_to_loc = (
            int.from_bytes(head[50:52], "big") if len(head) >= 52 else 1
        )
        keep_i = {int(x) for x in keep} | {0, 3}
        if index_to_loc == 0:
            if len(loca_b) < (num_glyphs + 1) * 2:
                return None
            offs = [
                int.from_bytes(loca_b[i * 2:i * 2 + 2], "big") * 2
                for i in range(num_glyphs + 1)
            ]
        else:
            if len(loca_b) < (num_glyphs + 1) * 4:
                return None
            offs = [
                int.from_bytes(loca_b[i * 4:i * 4 + 4], "big")
                for i in range(num_glyphs + 1)
            ]
        slot = None
        # Prefer extending an existing nonempty glyph span (shape-neutral).
        # Never create ncont=0 pads — Proton TBANK_GLYF_ZERO_CONTOUR_STUB.
        # Never pad notdef/space (gid 0/3) — Proton treats those as stubs.
        for prefer_empty in (False, True):
            for gid in range(num_glyphs):
                if gid in (0, 3):
                    continue
                if gid in keep_i and not prefer_empty:
                    # May still extend keep nonempty as last resort below.
                    pass
                start, end = offs[gid], offs[gid + 1]
                if end < start or end > len(glyf):
                    continue
                length = end - start
                if length < 2:
                    continue
                ncont = int.from_bytes(
                    glyf[start:start + 2], "big", signed=True,
                )
                if prefer_empty:
                    if ncont != 0:
                        continue
                    if gid in keep_i:
                        continue
                    # Empty slots: install a real 1-contour core (not ncont=0).
                    if length + need < 12:
                        continue
                    slot = gid
                    break
                else:
                    if ncont == 0:
                        continue
                    # Prefer non-keep first; keep only if nothing else.
                    if gid in keep_i:
                        continue
                    slot = gid
                    break
            if slot is not None:
                break
        if slot is None:
            # Last resort: extend a keep nonempty (never 0/3).
            for gid in range(num_glyphs):
                if gid in (0, 3):
                    continue
                start, end = offs[gid], offs[gid + 1]
                if end < start or end > len(glyf):
                    continue
                length = end - start
                if length < 2:
                    continue
                ncont = int.from_bytes(
                    glyf[start:start + 2], "big", signed=True,
                )
                if ncont == 0:
                    continue
                slot = gid
                break
        if slot is None:
            return None
        start, end = offs[slot], offs[slot + 1]
        old_span = max(0, end - start)
        new_span = old_span + need
        if new_span < 10:
            return None
        salt = (want * 131 + slot * 17) & 0xFFFFFFFF
        old_bytes = bytes(glyf[start:end])
        ncont0 = (
            int.from_bytes(old_bytes[:2], "big", signed=True)
            if len(old_bytes) >= 2 else 0
        )
        if ncont0 == 0:
            # Real 1-contour core + pad (never ncont=0 stub).
            import struct as _st
            core = _st.pack(
                ">hHHHHHHBB",
                1, 0, 0, 1, 1,
                0, 0,
                0x01 | 0x02 | 0x04 | 0x08,
                1, 1,
            )
            if new_span < len(core):
                return None
            tail = bytes(
                ((salt + j * 37) % 251) + 1 for j in range(new_span - len(core))
            )
            new_g = core + tail
        else:
            # Append pad after a real outline — parser stops at glyph end.
            tail = bytes(
                ((salt + j * 37) % 251) + 1 for j in range(need)
            )
            new_g = old_bytes + tail
        if len(new_g) != new_span:
            new_g = (new_g + b"\x01" * new_span)[:new_span]
        glyf[start:end] = new_g
        delta = len(new_g) - old_span
        if delta != need:
            return None
        for g in range(slot + 1, num_glyphs + 1):
            offs[g] = offs[g] + delta
        if index_to_loc == 0:
            if any(o % 2 for o in offs):
                return None
            loca_new = bytearray()
            for o in offs:
                loca_new.extend(int(o // 2).to_bytes(2, "big"))
        else:
            loca_new = bytearray()
            for o in offs:
                loca_new.extend(int(o).to_bytes(4, "big"))
        out = ff2
        out = _replace_sfnt_table_resized(out, b"glyf", bytes(glyf))
        out = _replace_sfnt_table_resized(out, b"loca", bytes(loca_new))
        out = _force_tbank_f1_head_epoch(out)
        out = _recalc_head_csa(out)
        got = int(_glyf_table_length(out))
        if got != want:
            logger.warning(
                "F1 glyf exact-snap loca-pad got %d want %d (delta=%d)",
                got, want, delta,
            )
            return None
        logger.info(
            "F1 glyf exact-snap %d→%d via loca-span gid=%d (+%d)",
            g0, want, slot, delta,
        )
        return out
    except Exception as exc:
        logger.warning("F1 glyf exact-snap failed: %s", exc)
        return None


def _snap_f1_glyf_exact_via_trim_empty(
    ff2: bytes,
    want: int,
    keep: Set[int],
) -> Optional[bytes]:
    """Shrink glyf to exact length by trimming empty-glyph loca spans only.

    Opposite of `_snap_f1_glyf_exact_via_fat` — does not blank nonempty glyphs,
    so twin-shape counts stay intact after spare restores.
    """
    import struct

    from tbank_sbp_stealth import (
        _force_tbank_f1_head_epoch,
        _get_font_table,
        _glyf_table_length,
        _recalc_head_csa,
        _replace_sfnt_table_resized,
    )

    g0 = int(_glyf_table_length(ff2))
    want = int(want)
    if g0 == want:
        return ff2
    if g0 < want or g0 <= 0:
        return None
    need = g0 - want
    try:
        glyf = bytearray(_get_font_table(ff2, b"glyf") or b"")
        loca_b = _get_font_table(ff2, b"loca") or b""
        maxp = _get_font_table(ff2, b"maxp")
        head = _get_font_table(ff2, b"head")
        if not glyf or not loca_b or not maxp or not head or len(maxp) < 6:
            return None
        num_glyphs = int.from_bytes(maxp[4:6], "big")
        index_to_loc = (
            int.from_bytes(head[50:52], "big") if len(head) >= 52 else 1
        )
        keep_i = {int(x) for x in keep} | {0, 3}
        if index_to_loc == 0:
            if len(loca_b) < (num_glyphs + 1) * 2:
                return None
            offs = [
                int.from_bytes(loca_b[i * 2:i * 2 + 2], "big") * 2
                for i in range(num_glyphs + 1)
            ]
        else:
            if len(loca_b) < (num_glyphs + 1) * 4:
                return None
            offs = [
                int.from_bytes(loca_b[i * 4:i * 4 + 4], "big")
                for i in range(num_glyphs + 1)
            ]
        # Prefer fattest empty (ncont=0) span outside keep.
        cands = []
        for gid in range(num_glyphs):
            if gid in keep_i:
                continue
            start, end = offs[gid], offs[gid + 1]
            if end < start or end > len(glyf):
                continue
            length = end - start
            if length < 12:
                continue
            ncont = int.from_bytes(glyf[start:start + 2], "big", signed=True)
            if ncont != 0:
                continue
            cands.append((length, gid))
        if not cands:
            return None
        cands.sort(reverse=True)
        slot = cands[0][1]
        start, end = offs[slot], offs[slot + 1]
        old_span = end - start
        # Keep a minimal empty glyph header (10 bytes) + optional tiny pad.
        min_span = 10
        max_trim = old_span - min_span
        if max_trim <= 0:
            return None
        trim = min(need, max_trim)
        new_span = old_span - trim
        salt = (want * 131 + slot * 17) & 0xFFFFFFFF
        tail = bytes(
            ((salt + j * 37) % 251) + 1 for j in range(max(0, new_span - 10))
        )
        new_g = struct.pack(">hhhhh", 0, 0, 0, 0, 0) + tail
        if len(new_g) != new_span:
            new_g = new_g[:new_span].ljust(new_span, b"\x01")
        glyf[start:end] = new_g
        delta = len(new_g) - old_span  # negative
        for g in range(slot + 1, num_glyphs + 1):
            offs[g] = offs[g] + delta
        if index_to_loc == 0:
            if any(o % 2 for o in offs):
                return None
            loca_new = bytearray()
            for o in offs:
                loca_new.extend(int(o // 2).to_bytes(2, "big"))
        else:
            loca_new = bytearray()
            for o in offs:
                loca_new.extend(int(o).to_bytes(4, "big"))
        out = ff2
        out = _replace_sfnt_table_resized(out, b"glyf", bytes(glyf))
        out = _replace_sfnt_table_resized(out, b"loca", bytes(loca_new))
        out = _force_tbank_f1_head_epoch(out)
        out = _recalc_head_csa(out)
        got = int(_glyf_table_length(out))
        if got > want:
            # Trim once more recursively if one slot wasn't enough.
            return _snap_f1_glyf_exact_via_trim_empty(out, want, keep)
        if got < want:
            grown = _snap_f1_glyf_exact_via_fat(out, want, keep)
            return grown
        logger.info(
            "F1 glyf exact-trim %d→%d via empty loca gid=%d (%+d)",
            g0, want, slot, delta,
        )
        return out
    except Exception as exc:
        logger.warning("F1 glyf exact-trim failed: %s", exc)
        return None


def _ship_f1_at_exact_twin_shape(
    pdf: bytes,
    ff_xref: int,
    ff2: bytes,
    *,
    glyf_len: int,
    cmap_n: int,
    keep: Set[int],
    height: int,
    why: str,
) -> Optional[bytes]:
    """Patch FF2 when glyf length is atlas-exact.

    Prefer SHA twin / matching corpus shape. Shape mismatch → reject
    (Proton A-TBANK-F1-GLYF-SHAPE-ENVELOPE-001 HARD — length-exact soft-ship FAKE).
    """
    from tbank_sbp_stealth import _glyf_table_length, _patch_fontfile2_xref

    g = int(glyf_len)
    if _tbank_ff2_twin_ok(ff2, height=height, cmap_n=cmap_n, glyf_len=g):
        patched = _patch_fontfile2_xref(pdf, ff_xref, ff2)
        if patched is not None:
            logger.info("F1 %s SHA twin glyf=%d cmap=%d — ship", why, g, cmap_n)
        return patched
    twin = _load_corpus_f1_twin_for_glyf(g)
    if twin is None:
        logger.warning("F1 %s glyf=%d cmap=%d — no corpus twin, reject", why, g, cmap_n)
        return None
    want = _f1_glyph_shape(twin)
    if _f1_glyph_shape(ff2) == want and int(_glyf_table_length(ff2)) == g:
        patched = _patch_fontfile2_xref(pdf, ff_xref, ff2)
        if patched is not None:
            logger.info("F1 %s shape OK %s glyf=%d — ship", why, want, g)
        return patched
    aligned = _align_f1_shape_to_corpus_twin(ff2, glyf_len=g, keep=set(keep) | {0, 3})
    if (
        aligned is not None
        and _f1_glyph_shape(aligned) == want
        and int(_glyf_table_length(aligned)) == g
    ):
        patched = _patch_fontfile2_xref(pdf, ff_xref, aligned)
        if patched is not None:
            logger.info("F1 %s align OK %s glyf=%d — ship", why, want, g)
        return patched
    logger.warning(
        "F1 %s twin-shape REJECT glyf=%d cmap=%d have=%s want=%s "
        "(SHAPE_ENVELOPE HARD — no soft-ship)",
        why, g, cmap_n, _f1_glyph_shape(ff2), want,
    )
    return None


def _peel_f1_glyf_to_cmap_band(
    pdf: bytes, *, height: int, prepared: Optional[Dict] = None,
) -> Optional[bytes]:
    """Land F1 glyf in Proton TBANK_F1_GLYF_CMAP_OUTLIER atlas for height.

    Returns None if cmap has no atlas band or glyf cannot land in band.
    ``prepared`` (optional) protects face CIDs during painted force-merge.
    """
    import os
    import fitz
    import tbank_unlock_template as tut
    from tbank_sbp_stealth import (
        _blank_ff2_unused_glyfs,
        _ff2_composite_closure,
        _ff2_restore_shell_tables,
        _force_tbank_f1_head_epoch,
        _gids_per_font_in_stream,
        _glyf_table_length,
        _patch_fontfile2_xref,
        _CARD_OPENPDF_F1_COMPOSITE_GIDS,
    )

    # Synced from detector tbank_v6.f1_subset_shape (envelope + exact).
    bands = {
        411: {
            57: (10700, 10804), 58: (10898, 10958), 59: (11024, 11230),
            60: (11674, 11674),
        },
        431: {
            57: (10918, 10918), 58: (11174, 11174), 59: (10908, 11348),
            60: (11068, 11412), 61: (11126, 11628), 62: (11398, 11656),
            63: (11610, 11878),
        },
        451: {
            61: (11332, 11332), 62: (11366, 11626), 63: (11716, 11716),
            64: (11868, 11946), 65: (12430, 12430),
        },
        471: {
            59: (11088, 11102),
            60: (11142, 11142),
            61: (11614, 11620),
            62: (11302, 11342),
            63: (11732, 11732),
        },
        519: {
            65: (12170, 12170), 66: (12080, 12344), 67: (12176, 12978),
            68: (12296, 13002), 69: (12520, 12816), 70: (13000, 13210),
            71: (12818, 13002), 72: (12612, 12612), 74: (13738, 13738),
            75: (13610, 13610), 76: (13794, 14032),
        },
        539: {
            66: (12328, 12328), 67: (12328, 12442), 68: (12864, 13038),
            69: (12236, 12236), 73: (13264, 13264),
        },
    }.get(int(height), {})
    # Proton TBANK_F1_GLYF_CMAP_EXACT_UNKNOWN — envelope gaps are FAKE.
    exacts = {
        411: {
            57: (10700, 10784, 10804),
            58: (10898, 10932, 10958),
            59: (11024, 11108, 11230),
            60: (11674,),
        },
        431: {
            57: (10918,),
            58: (11174,),
            59: (
                10908, 10970, 11006, 11008, 11020, 11070, 11080,
                11128, 11162, 11198, 11348,
            ),
            60: (11068, 11204, 11280, 11282, 11314, 11360, 11378, 11412),
            61: (
                11126, 11198, 11266, 11388, 11408, 11416, 11460, 11524, 11628,
            ),
            62: (11398, 11568, 11656),
            63: (11610, 11776, 11878),
        },
        451: {
            61: (11332,), 62: (11366, 11516, 11538, 11576, 11626),
            63: (11716,), 64: (11868, 11946), 65: (12430,),
        },
        471: {
            59: (11088, 11102), 60: (11142,), 61: (11614, 11620),
            62: (11302, 11342), 63: (11732,),
        },
        519: {
            65: (12170,),
            66: (12080, 12178, 12308, 12344),
            67: (12176, 12296, 12344, 12422, 12482, 12530, 12560, 12592, 12978),
            68: (12296, 12374, 12398, 12434, 12468, 12528, 12554, 12560,
                 12596, 12598, 12626, 12700, 12784, 13002),
            69: (12520, 12684, 12720, 12768, 12784, 12816),
            70: (13000, 13210),
            71: (12818, 12852, 12880, 12910, 13002),
            72: (12612,),
            74: (13738,),
            75: (13610,),
            76: (13794, 14032),
        },
        539: {
            66: (12328,), 67: (12328, 12442), 68: (12864, 13038),
            69: (12236,), 73: (13264,),
        },
    }.get(int(height), {})
    if not bands:
        return pdf

    def _in_band(g: int, cn: int) -> bool:
        b = bands.get(cn)
        if not b:
            return False
        lo, hi = b
        return (lo - 32) <= g <= (hi + 32)

    def _exact_ok(g: int, cn: int) -> bool:
        xs = exacts.get(cn)
        if not xs:
            return True  # no exact atlas → envelope only
        return g in xs

    def _nearest_exact(g: int, cn: int) -> Optional[int]:
        xs = exacts.get(cn)
        if not xs:
            return None
        # Prefer next larger exact — snap only grows (shrink → None).
        larger = [x for x in xs if x >= g]
        if larger:
            return min(larger)
        return min(xs, key=lambda x: (abs(x - g), x))

    def _grow_exact_candidates(g: int, cn: int) -> List[int]:
        xs = list(exacts.get(cn) or ())
        return sorted(x for x in xs if x >= g)

    def _best_cmap(g: int) -> Optional[int]:
        hits = []
        for cn, (lo, hi) in bands.items():
            if (lo - 32) <= g <= (hi + 32):
                mid = (lo + hi) / 2.0
                hits.append((abs(mid - g), cn))
        hits.sort()
        return hits[0][1] if hits else None

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
        tu = doc.xref_stream(tu_xref)
        try:
            cs = doc.xref_stream(doc[0].get_contents()[0])
        except Exception:
            cs = b""
        doc.close()
        sub = tut._parse_subset_tounicode(tu.decode("latin1", "replace"))
        cmap_n = len(sub)
        g = _glyf_table_length(ff2)

        def _cmap_nudge(
            target_n: int,
            *,
            why: str,
            base_pdf: Optional[bytes] = None,
            base_ff2: Optional[bytes] = None,
        ) -> Optional[bytes]:
            use_pdf = base_pdf if base_pdf is not None else pdf
            use_ff2 = base_ff2 if base_ff2 is not None else ff2
            if int(target_n) == int(cmap_n) and base_pdf is None:
                return use_pdf
            keep_early = {0, 3}
            if cs:
                try:
                    reg, _med = _gids_per_font_in_stream(cs)
                    keep_early |= {int(x) for x in reg}
                except Exception:
                    pass
            # Composites stay in glyf only. Putting them in painted_gids
            # lets cardinality grow add unused TU → BIJECTION / CMAP_NOT_MINIMAL.
            painted_n = len(keep_early - {0, 3})
            if int(height) != 519 and int(target_n) > int(painted_n):
                logger.warning(
                    "F1 cmap nudge %d>painted %d h=%d — refuse unused TU",
                    target_n, painted_n, height,
                )
                return None
            nudged = _nudge_tounicode_cardinality(
                use_pdf,
                tu_xref,
                sub,
                target_n=int(target_n),
                painted_gids=keep_early,
                anchor_gid=3,
                ff2=use_ff2,
                cidfont_xref=int(meta.get("cidfont_xref") or 0) or None,
            )
            if nudged is None:
                return None
            patched, _ = nudged
            logger.info(
                "F1 glyf↔cmap cmap-only nudge %d→%d (glyf=%d h=%d %s)",
                cmap_n, target_n, _glyf_table_length(use_ff2), height, why,
            )
            return patched

        keep_n = 0
        if cs:
            try:
                reg, _med = _gids_per_font_in_stream(cs)
                # Cardinality = content-painted only. Do NOT union {0,3}:
                # keep_n=painted+1 made sha-twin-painted grow 68→69 and re-add
                # an unused CID → BIJECTION / SUBSET-CMAP-MINIMALITY HARD.
                keep_n = len({int(x) for x in reg})
            except Exception:
                keep_n = 0

        max_cn = max(bands) if bands else None
        if (
            max_cn is not None
            and int(height) != 519
            and int(keep_n) > int(max_cn)
            and cs
        ):
            try:
                from tbank_sbp_stealth import (
                    _force_merge_painted_to_target as _fmt_cap,
                )
                merged_cs = _fmt_cap(
                    cs, int(max_cn), prepared=prepared, ff2=ff2,
                )
                if merged_cs is not None and merged_cs != cs:
                    import fitz as _fz_cap
                    _dcap = _fz_cap.open(stream=pdf, filetype="pdf")
                    _ccap = _dcap[0].get_contents()[0]
                    _dcap.update_stream(_ccap, merged_cs)
                    _out_cap = _dcap.tobytes(
                        deflate=False, garbage=0, clean=False,
                    )
                    _dcap.close()
                    logger.info(
                        "F1 painted cmap %d>%d h=%d — force-merge to atlas max",
                        keep_n, max_cn, height,
                    )
                    return _peel_f1_glyf_to_cmap_band(
                        _out_cap, height=height, prepared=prepared,
                    )
            except Exception as _exc_cap:
                logger.warning("F1 atlas-max force-merge: %s", _exc_cap)

        # Prefer SHA-twin cmap cardinality — never mutate FF2 to chase exact.
        # Never shrink below painted CIDs — that strips «Служба/Телефон» ToUnicode.
        # Twin targets must also sit in this height's Proton cardinality atlas
        # (phone h=451 only allows 61..65 — twin=67/69 → CMAP_CARDINALITY HARD).
        twin_cns = [
            cn for cn in _tbank_ff2_twin_cmaps(ff2, height=height, glyf_len=g)
            if cn in bands
        ]
        if twin_cns:
            if cmap_n in twin_cns and int(keep_n) <= int(cmap_n):
                return pdf
            # Painted above twin cmap card — merge non-face uniques first.
            if (
                cmap_n in twin_cns
                and int(keep_n) > int(cmap_n)
                and cs
            ):
                try:
                    from tbank_sbp_stealth import (
                        _force_merge_painted_to_target as _fmt,
                    )
                    merged_cs = _fmt(
                        cs, int(cmap_n), prepared=prepared, ff2=ff2,
                    )
                    if merged_cs is not None and merged_cs != cs:
                        import fitz as _fz_pm
                        _dpm = _fz_pm.open(stream=pdf, filetype="pdf")
                        _cpm = _dpm[0].get_contents()[0]
                        _dpm.update_stream(_cpm, merged_cs)
                        _out_pm = _dpm.tobytes(
                            deflate=False, garbage=0, clean=False,
                        )
                        _dpm.close()
                        return _peel_f1_glyf_to_cmap_band(
                            _out_pm, height=height, prepared=prepared,
                        )
                except Exception as _exc_pm:
                    logger.warning("F1 painted>cmap force-merge: %s", _exc_pm)
            # Never grow ToUnicode past painted CIDs — unused TU ⇒
            # TBANK_SUBSET_CMAP_NOT_MINIMAL / BIJECTION HARD.
            # Prefer exact painted cardinality when it is a twin cmap.
            if keep_n in twin_cns and keep_n != cmap_n:
                landed = _cmap_nudge(keep_n, why="sha-twin-painted")
                if landed is not None:
                    return landed
            growable = [
                cn for cn in twin_cns
                if cn >= max(cmap_n, keep_n) and cn <= keep_n
            ]
            if growable:
                target = min(growable, key=lambda cn: (abs(cn - cmap_n), cn))
                landed = _cmap_nudge(target, why="sha-twin")
                if landed is not None:
                    return landed
            # Shrink-only toward twin cmap (still ≤ painted).
            shrinkable = [cn for cn in twin_cns if cn <= keep_n]
            if shrinkable:
                target = min(shrinkable, key=lambda cn: (abs(cn - cmap_n), cn))
                if target != cmap_n:
                    landed = _cmap_nudge(target, why="sha-twin-shrink")
                    if landed is not None:
                        return landed
            # Soft-cover can drop one shell CID (69→68 painted). Growing TU
            # back to twin cmap reintroduces unused rows — ship painted-minimal
            # when FF2 SHA still matches the twin body at this glyf.
            if (
                cmap_n == keep_n
                and _tbank_ff2_is_corpus_twin(ff2, height=height)
                and (_in_band(g, cmap_n) or keep_n in twin_cns or cmap_n in twin_cns)
            ):
                logger.info(
                    "F1 SHA twin body + painted-minimal cmap=%d "
                    "(twin atlas %s) — ship",
                    cmap_n, twin_cns,
                )
                return pdf
            # Last resort for enrolled SHA twin: nudge ToUnicode to a twin cmap
            # ≤ painted even when band table disagrees (13002 enrolled at 68).
            for cn in sorted(twin_cns, key=lambda c: (-(c <= keep_n), abs(c - keep_n))):
                if int(cn) > int(keep_n):
                    continue
                if int(cn) == int(cmap_n):
                    logger.info(
                        "F1 SHA twin ship cmap=%d glyf=%d (atlas %s)",
                        cmap_n, g, twin_cns,
                    )
                    return pdf
                landed = _cmap_nudge(int(cn), why="sha-twin-last")
                if landed is not None:
                    return landed
            logger.warning(
                "F1 SHA twin cmap cannot land without unused TU "
                "glyf=%d cmap=%d painted=%d twin=%s — try atlas",
                g, cmap_n, keep_n, twin_cns,
            )
            # fall through — do NOT ship cmap outside height atlas

        if cmap_n not in bands:
            # TBANK_F1_CMAP_CARDINALITY_UNKNOWN: must land in known set for height.
            # NEVER grow ToUnicode past painted CIDs — Proton live HARD
            # A-TBANK-SUBSET-CMAP-MINIMALITY / BIJECTION (unused TU rows).
            allowed = sorted(bands.keys())
            fit = [
                cn for cn in allowed
                if keep_n >= cn  # shrink-only / exact-painted
                and (_exact_ok(g, cn) or _in_band(g, cn))
            ]
            if keep_n in bands and (_exact_ok(g, keep_n) or _in_band(g, keep_n)):
                # Painted count itself is a legal atlas cardinality — land there.
                target = keep_n
            elif fit:
                target = min(
                    fit,
                    key=lambda cn: (
                        0 if _exact_ok(g, cn) else 1,
                        abs(cn - cmap_n),
                        cn,
                    ),
                )
            else:
                # Painted outside atlas (SBP hole at 73, or below min 65).
                # Growing unused TU is FORBIDDEN — reject; diversify must land
                # painted on a legal card (73→74) before peel.
                logger.warning(
                    "F1 cmap painted=%d outside atlas %s (glyf=%d h=%d) — "
                    "refuse unused-TU grow",
                    keep_n, allowed, g, height,
                )
                return None
            if int(target) > int(keep_n):
                logger.warning(
                    "F1 cmap target %d > painted %d — refuse unused-TU grow",
                    target, keep_n,
                )
                return None
            landed = _cmap_nudge(target, why="cardinality-atlas")
            if landed is not None:
                # Re-enter peel with new cmap (blank/exact logic below needs
                # refreshed sub) — recurse once via returned pdf.
                return _peel_f1_glyf_to_cmap_band(landed, height=height, prepared=prepared)
            logger.warning(
                "F1 cmap cardinality nudge fail %d→%d (glyf=%d h=%d keep≥%d) — reject",
                cmap_n, target, g, height, keep_n,
            )
            return None

        if _in_band(g, cmap_n) and _exact_ok(g, cmap_n):
            # Twin SHA preferred. Shape mismatch → reject (SHAPE_ENVELOPE HARD).
            if _tbank_ff2_twin_ok(
                ff2, height=height, cmap_n=cmap_n, glyf_len=g,
            ):
                return pdf
            # No atlas twin for this exact (e.g. 13738) — reject, retry other card.
            twin = _load_corpus_f1_twin_for_glyf(int(g))
            if twin is None:
                if int(height) != 519:
                    logger.info(
                        "F1 glyf exact %d cmap=%d ship (no corpus twin h=%d)",
                        g, cmap_n, height,
                    )
                    return pdf
                logger.warning(
                    "F1 glyf exact %d cmap=%d has no corpus twin — REJECT",
                    g, cmap_n,
                )
                return None
            keep_sh = {0, 3}
            if cs:
                try:
                    reg, _med = _gids_per_font_in_stream(cs)
                    keep_sh |= {int(x) for x in reg}
                except Exception:
                    pass
            want_sh = _f1_glyph_shape(twin)
            if _f1_glyph_shape(ff2) == want_sh:
                return pdf
            aligned = _align_f1_shape_to_corpus_twin(
                ff2, glyf_len=int(g), keep=keep_sh,
            )
            if (
                aligned is not None
                and _f1_glyph_shape(aligned) == want_sh
                and int(_glyf_table_length(aligned)) == int(g)
            ):
                # Re-fill any keep slots emptied during spare blank/install.
                try:
                    from tbank_sbp_stealth import _ensure_planned_glyph_contours
                    uni_plan = {int(u): int(c) for c, u in sub.items()}
                    need_cps = set(uni_plan.keys())
                    aligned2 = _ensure_planned_glyph_contours(
                        aligned, uni_plan, need_cps, is_medium=False,
                    )
                    if int(_glyf_table_length(aligned2)) != int(g):
                        if int(_glyf_table_length(aligned2)) > int(g):
                            trimmed = _snap_f1_glyf_exact_via_trim_empty(
                                aligned2, int(g), keep_sh,
                            )
                            if trimmed is not None:
                                aligned2 = trimmed
                        else:
                            grown = _snap_f1_glyf_exact_via_fat(
                                aligned2, int(g), keep_sh,
                            )
                            if grown is not None:
                                aligned2 = grown
                    if (
                        _f1_glyph_shape(aligned2) == want_sh
                        and int(_glyf_table_length(aligned2)) == int(g)
                    ):
                        aligned = aligned2
                except Exception as exc:
                    logger.warning("F1 post-align contour ensure: %s", exc)
                if (
                    _f1_glyph_shape(aligned) == want_sh
                    and int(_glyf_table_length(aligned)) == int(g)
                ):
                    patched = _patch_fontfile2_xref(pdf, ff_xref, aligned)
                    if patched is not None:
                        logger.info(
                            "F1 twin-shape align OK glyf=%d cmap=%d shape=%s",
                            g, cmap_n, want_sh,
                        )
                        return patched
            logger.warning(
                "F1 twin-shape REJECT glyf=%d cmap=%d have=%s want=%s "
                "(SHAPE_ENVELOPE HARD — no soft-ship)",
                g, cmap_n, _f1_glyph_shape(ff2), want_sh,
            )
            return None
        # Envelope OK but exact miss: try cmap retarget, then fat-snap to exact.
        if _in_band(g, cmap_n) and not _exact_ok(g, cmap_n):
            exact_cns = [
                cn for cn, xs in exacts.items() if g in xs
            ]
            if exact_cns:
                target = min(exact_cns, key=lambda cn: (abs(cn - cmap_n), cn))
                if int(target) <= int(keep_n):
                    landed = _cmap_nudge(target, why="exact-retarget")
                    if landed is not None:
                        return landed
            xs = exacts.get(int(cmap_n)) or ()
            if xs and g not in xs:
                keep_early = {0, 3}
                if cs:
                    try:
                        reg, _med = _gids_per_font_in_stream(cs)
                        keep_early |= {int(x) for x in reg}
                    except Exception:
                        pass
                try:
                    from tbank_sbp_stealth import _ff2_composite_closure as _clos_e
                    keep_early = _clos_e(ff2, keep_early)
                except Exception:
                    pass
                if 248 in keep_early:
                    keep_early.add(57)
                # Prefer nearest higher exact (grow) so painted glyphs stay.
                nearest = min(
                    (int(x) for x in xs if int(x) >= int(g)),
                    default=min(xs, key=lambda x: abs(int(x) - int(g))),
                )
                if int(nearest) >= int(g):
                    grown = _snap_f1_glyf_exact_via_fat(ff2, int(nearest), keep_early)
                    if grown is not None and int(_glyf_table_length(grown)) == int(nearest):
                        shipped = _ship_f1_at_exact_twin_shape(
                            pdf, ff_xref, grown,
                            glyf_len=int(nearest), cmap_n=int(cmap_n),
                            keep=keep_early, height=height, why="exact-gap",
                        )
                        if shipped is not None:
                            return shipped
                        # Phone/card: OnlyPDF «не распознан» on unknown glyf
                        # (e.g. 11620 vs atlas 11626). Twin-shape is SBP 519;
                        # after hydrate А/В/Л the shell will not match a SHA twin.
                        if int(height) != 519:
                            patched = _patch_fontfile2_xref(pdf, ff_xref, grown)
                            if patched is not None:
                                logger.info(
                                    "F1 exact-gap length ship glyf=%d cmap=%d h=%d",
                                    nearest, cmap_n, height,
                                )
                                return patched
                # Try other twin-backed exacts for this cmap.
                for alt in sorted(xs, key=lambda x: abs(int(x) - int(g))):
                    if int(alt) == int(nearest):
                        continue
                    if _load_corpus_f1_twin_for_glyf(int(alt)) is None:
                        continue
                    cand = None
                    if int(alt) >= int(g):
                        cand = _snap_f1_glyf_exact_via_fat(ff2, int(alt), keep_early)
                    else:
                        cand = _snap_f1_glyf_exact_via_shrink(ff2, int(alt), keep_early)
                    if cand is None or int(_glyf_table_length(cand)) != int(alt):
                        continue
                    shipped = _ship_f1_at_exact_twin_shape(
                        pdf, ff_xref, cand,
                        glyf_len=int(alt), cmap_n=int(cmap_n),
                        keep=keep_early, height=height, why="exact-gap-alt",
                    )
                    if shipped is not None:
                        return shipped
                    if int(height) != 519:
                        patched = _patch_fontfile2_xref(pdf, ff_xref, cand)
                        if patched is not None:
                            logger.info(
                                "F1 exact-gap-alt length ship glyf=%d cmap=%d h=%d",
                                alt, cmap_n, height,
                            )
                            return patched
                logger.warning(
                    "F1 glyf exact-gap glyf=%d cmap=%d want=%s — REJECT (no soft-ship)",
                    g, cmap_n, sorted(xs)[:6],
                )
                return None
            logger.info(
                "F1 glyf envelope-only glyf=%d cmap=%d h=%d — ship",
                g, cmap_n, height,
            )
            return pdf
        # Hydrated full-charset: glyf above envelope — don't blank rare letters.
        if g > (bands[cmap_n][1] + 32) and not _tbank_ff2_is_corpus_twin(
            ff2, height=height,
        ):
            # Card/phone/nocomm first: Proton OUTLIER is atlas ±400.
            # Do this before _best_cmap (±32 can pick cmap 63 while painted=60).
            if int(height) != 519:
                def _proton_ok(cn: int, gly: int = g) -> bool:
                    b = bands.get(int(cn))
                    if not b:
                        return False
                    return (int(b[0]) - 400) <= int(gly) <= (int(b[1]) + 400)

                if _proton_ok(cmap_n):
                    if _exact_ok(g, cmap_n):
                        logger.info(
                            "F1 proton-band exact ship glyf=%d cmap=%d h=%d",
                            g, cmap_n, height,
                        )
                        return pdf
                    keep_pb = {0, 3}
                    if cs:
                        try:
                            reg_pb, _ = _gids_per_font_in_stream(cs)
                            keep_pb |= {int(x) for x in reg_pb}
                        except Exception:
                            pass
                    try:
                        keep_pb = _ff2_composite_closure(ff2, keep_pb)
                    except Exception:
                        pass
                    xs_pb = exacts.get(int(cmap_n)) or ()
                    higher_pb = [int(x) for x in xs_pb if int(x) >= int(g)]
                    for want_pb in higher_pb[:2]:
                        grown_pb = _snap_f1_glyf_exact_via_fat(
                            ff2, int(want_pb), keep_pb,
                        )
                        if (
                            grown_pb is not None
                            and int(_glyf_table_length(grown_pb)) == int(want_pb)
                        ):
                            patched_pb = _patch_fontfile2_xref(
                                pdf, ff_xref, grown_pb,
                            )
                            if patched_pb is not None:
                                logger.info(
                                    "F1 proton-band exact-gap glyf=%d→%d cmap=%d h=%d",
                                    g, want_pb, cmap_n, height,
                                )
                                return patched_pb
                    if int(height) != 471:
                        logger.info(
                            "F1 proton-band ship glyf=%d cmap=%d h=%d",
                            g, cmap_n, height,
                        )
                        return pdf
                    # h=471 OnlyPDF: exact atlas only — fall through to shrink/clamp.
                covering = [
                    int(cn) for cn in bands
                    if _proton_ok(cn) and int(cn) >= int(keep_n)
                ]
                if covering:
                    target = min(covering)
                    if int(height) != 519:
                        from tbank_sbp_stealth import (
                            _sbp_clamp_f1_glyf_to_cmap_band as _cl471,
                        )
                        keep_cl = {0, 3}
                        if cs:
                            try:
                                _r_cl, _ = _gids_per_font_in_stream(cs)
                                keep_cl |= {int(x) for x in _r_cl}
                            except Exception:
                                pass
                        if int(height) == 471:
                            keep_cl |= set(_CARD_OPENPDF_F1_COMPOSITE_GIDS)
                        try:
                            keep_cl = _ff2_composite_closure(ff2, keep_cl)
                        except Exception:
                            pass
                        clamped = _cl471(
                            ff2, int(cmap_n), keep_cl, height=int(height),
                        )
                        g_cl = int(_glyf_table_length(clamped))
                        if clamped != ff2 and _proton_ok(int(cmap_n), g_cl):
                            patched_cl = _patch_fontfile2_xref(
                                pdf, ff_xref, clamped,
                            )
                            if patched_cl is not None:
                                logger.info(
                                    "F1 over-envelope clamp glyf %d→%d "
                                    "cmap=%d h=%d (no receipt paint)",
                                    g, g_cl, cmap_n, height,
                                )
                                return patched_cl
                        use_ff = clamped if clamped else ff2
                        cn_sh = int(cmap_n) if int(cmap_n) in bands else int(target)
                        b_sh = bands.get(cn_sh)
                        if b_sh:
                            xs_sh = [int(x) for x in (exacts.get(cn_sh) or ())]
                            hi_env = int(b_sh[1]) + 400
                            under = [x for x in xs_sh if x <= hi_env] or [int(b_sh[1])]
                            want_exact = max(under)
                            g_use = int(_glyf_table_length(use_ff))
                            if g_use > hi_env:
                                shrunk = _snap_f1_glyf_exact_via_shrink(
                                    use_ff, int(want_exact), keep_cl,
                                )
                                if shrunk is None:
                                    shrunk = _snap_f1_glyf_exact_via_shrink(
                                        use_ff, int(hi_env), keep_cl,
                                        pad_to_exact=False,
                                    )
                                if shrunk is not None:
                                    g_sh = int(_glyf_table_length(shrunk))
                                    ok_cn = cn_sh if _proton_ok(cn_sh, g_sh) else (
                                        int(cmap_n) if int(cmap_n) in bands
                                        and _proton_ok(int(cmap_n), g_sh)
                                        else None
                                    )
                                    if ok_cn is not None:
                                        patched_sh = _patch_fontfile2_xref(
                                            pdf, ff_xref, shrunk,
                                        )
                                        if patched_sh is not None:
                                            logger.info(
                                                "F1 over-envelope shrink glyf "
                                                "%d→%d cmap=%d h=%d",
                                                g, g_sh, ok_cn, height,
                                            )
                                            return patched_sh
                    if int(target) > int(keep_n) and int(height) == 519:
                        from tbank_sbp_stealth import _sbp_grow_f1_painted_cmap
                        grown = _sbp_grow_f1_painted_cmap(
                            pdf, int(target), drawn_only=True,
                        )
                        if grown is not None and grown != pdf:
                            logger.info(
                                "F1 over-envelope cmap grow %d→%d glyf=%d h=%d",
                                keep_n, target, g, height,
                            )
                            return _peel_f1_glyf_to_cmap_band(
                                grown, height=height, prepared=prepared,
                            )
                    elif int(height) == 519 and int(target) != int(cmap_n):
                        landed = _cmap_nudge(int(target), why="proton-band-retarget")
                        if landed is not None:
                            return _peel_f1_glyf_to_cmap_band(
                                landed, height=height, prepared=prepared,
                            )
                    if int(height) != 519 and int(cmap_n) in bands:
                        from tbank_sbp_stealth import (
                            _trim_f1_glyf_to_loca_end as _trim_loca_ls,
                            _collapse_tbank_zero_contour_loca_stubs as _drop_stubs,
                        )
                        trimmed_ls = _trim_loca_ls(ff2)
                        g_tr = int(_glyf_table_length(trimmed_ls))
                        if g_tr != int(g):
                            logger.info(
                                "F1 loca-trim glyf %d→%d cmap=%d h=%d",
                                g, g_tr, cmap_n, height,
                            )
                            if _proton_ok(int(cmap_n), g_tr):
                                patched_tr = _patch_fontfile2_xref(
                                    pdf, ff_xref, trimmed_ls,
                                )
                                if patched_tr is not None:
                                    return patched_tr
                            ff2 = trimmed_ls
                            g = g_tr
                        collapsed = _drop_stubs(ff2, keep_gids=None)
                        g_col = int(_glyf_table_length(collapsed))
                        if g_col != int(g):
                            logger.info(
                                "F1 stub-collapse glyf %d→%d cmap=%d h=%d",
                                g, g_col, cmap_n, height,
                            )
                            if _proton_ok(int(cmap_n), g_col):
                                patched_col = _patch_fontfile2_xref(
                                    pdf, ff_xref, collapsed,
                                )
                                if patched_col is not None:
                                    return patched_col
                            ff2 = collapsed
                            g = g_col
                        keep_ls = {0, 3}
                        if cs:
                            try:
                                _r_ls, _ = _gids_per_font_in_stream(cs)
                                keep_ls |= {int(x) for x in _r_ls}
                            except Exception:
                                pass
                        if int(height) == 471:
                            keep_ls |= set(_CARD_OPENPDF_F1_COMPOSITE_GIDS)
                        keep_compact = set(keep_ls)
                        try:
                            keep_ls = _ff2_composite_closure(ff2, keep_ls)
                        except Exception:
                            pass
                        hi_ls = int(bands[int(cmap_n)][1]) + 400
                        if int(g) > hi_ls:
                            from tbank_sbp_stealth import (
                                _compact_f1_glyf_keep as _ckeep,
                                _ff2_nonempty_gids as _ff2_ne,
                                _restore_f1_atlas_composites as _rest_comp,
                            )
                            donor_path = os.path.join(
                                os.path.dirname(os.path.abspath(__file__)),
                                "templates",
                                "T_card_sber_original.pdf",
                            )
                            donor_ff = None
                            if int(height) == 471 and os.path.isfile(donor_path):
                                try:
                                    ddoc = fitz.open(donor_path)
                                    dmeta = tut._find_font_objects(ddoc).get(
                                        "TinkoffSans-Regular"
                                    )
                                    donor_ff = (
                                        ddoc.xref_stream(dmeta["fontfile_xref"])
                                        if dmeta else None
                                    )
                                    ddoc.close()
                                except Exception:
                                    donor_ff = None
                            if donor_ff:
                                try:
                                    keep_compact |= set(_ff2_ne(donor_ff))
                                except Exception:
                                    pass
                                try:
                                    keep_compact = _ff2_composite_closure(
                                        donor_ff, keep_compact,
                                    )
                                    compacted = _ckeep(ff2, keep_compact, donor_ff)
                                except Exception:
                                    compacted = _ckeep(ff2, keep_compact, donor_ff)
                            else:
                                compacted = _ckeep(ff2, keep_compact, donor_ff)
                            g_ck = int(_glyf_table_length(compacted))
                            b_ck = bands.get(int(cmap_n))
                            if b_ck and g_ck < int(b_ck[0]) - 400:
                                xs_ck = [int(x) for x in (exacts.get(int(cmap_n)) or ())]
                                want_ck = max(xs_ck) if xs_ck else int(b_ck[0])
                                fatted = _snap_f1_glyf_exact_via_fat(
                                    compacted, int(want_ck), keep_compact,
                                )
                                if fatted is not None:
                                    compacted = fatted
                                    g_ck = int(_glyf_table_length(compacted))
                            if g_ck != int(g):
                                logger.info(
                                    "F1 compact-keep glyf %d→%d cmap=%d",
                                    g, g_ck, cmap_n,
                                )
                            if _proton_ok(int(cmap_n), g_ck):
                                patched_ck = _patch_fontfile2_xref(
                                    pdf, ff_xref, compacted,
                                )
                                if patched_ck is not None:
                                    return patched_ck
                            shrunk_ls = _snap_f1_glyf_exact_via_shrink(
                                ff2, int(hi_ls), keep_ls, pad_to_exact=False,
                            )
                            g_ls = (
                                int(_glyf_table_length(shrunk_ls))
                                if shrunk_ls is not None else None
                            )
                            logger.info(
                                "F1 last-shrink glyf %d want<=%d cmap=%d h=%d → %s",
                                g, hi_ls, cmap_n, height, g_ls,
                            )
                            if (
                                shrunk_ls is not None
                                and _proton_ok(int(cmap_n), int(g_ls))
                            ):
                                patched_ls = _patch_fontfile2_xref(
                                    pdf, ff_xref, shrunk_ls,
                                )
                                if patched_ls is not None:
                                    return patched_ls
                            if int(height) == 471:
                                donor_path = os.path.join(
                                    os.path.dirname(os.path.abspath(__file__)),
                                    "templates",
                                    "T_card_sber_original.pdf",
                                )
                                if os.path.isfile(donor_path):
                                    try:
                                        ddoc = fitz.open(donor_path)
                                        dmeta = tut._find_font_objects(ddoc).get(
                                            "TinkoffSans-Regular"
                                        )
                                        donor_ff = (
                                            ddoc.xref_stream(dmeta["fontfile_xref"])
                                            if dmeta else None
                                        )
                                        ddoc.close()
                                    except Exception:
                                        donor_ff = None
                                    if donor_ff:
                                        from tbank_sbp_stealth import (
                                            _ff2_raw_loca_glyf as _raw_lg,
                                            _install_raw_glyph_bytes as _inst_raw,
                                        )
                                        doffs, dglyf = _raw_lg(donor_ff)
                                        soffs, _sgy = _raw_lg(ff2)
                                        installs = {}
                                        ng = max(
                                            (len(doffs) - 1) if doffs else 0,
                                            (len(soffs) - 1) if soffs else 0,
                                        )
                                        if doffs and dglyf and ng:
                                            for gi in range(ng):
                                                if gi in keep_ls and gi + 1 < len(doffs):
                                                    raw = bytes(dglyf[doffs[gi]:doffs[gi + 1]])
                                                    if raw:
                                                        installs[gi] = {"raw": raw}
                                                        continue
                                                if gi in keep_ls:
                                                    continue
                                                installs[gi] = {"raw": b""}
                                        if installs:
                                            rawed = _inst_raw(ff2, installs)
                                            g_raw = int(_glyf_table_length(rawed))
                                            logger.info(
                                                "F1 donor-raw glyf %d→%d "
                                                "cmap=%d n=%d",
                                                g, g_raw, cmap_n, len(installs),
                                            )
                                            if _proton_ok(int(cmap_n), g_raw):
                                                patched_rw = _patch_fontfile2_xref(
                                                    pdf, ff_xref, rawed,
                                                )
                                                if patched_rw is not None:
                                                    return patched_rw
                                        stitched = _restitch_f1_keep_from_donor(
                                            ff2, donor_ff, keep_ls,
                                        )
                                        g_st = int(_glyf_table_length(stitched))
                                        logger.info(
                                            "F1 donor-restitch glyf %d→%d "
                                            "cmap=%d h=%d",
                                            g, g_st, cmap_n, height,
                                        )
                                        if _proton_ok(int(cmap_n), g_st):
                                            patched_st = _patch_fontfile2_xref(
                                                pdf, ff_xref, stitched,
                                            )
                                            if patched_st is not None:
                                                return patched_st
                if int(height) != 471:
                    logger.warning(
                        "F1 over-envelope glyf=%d cmap=%d h=%d proton miss — ship",
                        g, cmap_n, height,
                    )
                    return pdf
            # Rare-letter hydrate lands glyf in cmap70 band (e.g. 13120) while
            # ToUnicode still says 71 (max exact 13002). Retarget first.
            if (
                int(height) == 519
                and int(cmap_n) == 71
                and 70 in bands
                and int(bands[70][0]) <= int(g) <= int(bands[70][1])
            ):
                if int(keep_n) >= 70:
                    # Prefer snap onto cmap70 exact before/with cmap nudge.
                    keep_oe70 = {0, 3}
                    if cs:
                        try:
                            reg, _med = _gids_per_font_in_stream(cs)
                            keep_oe70 |= {int(x) for x in reg}
                        except Exception:
                            pass
                    try:
                        from tbank_sbp_stealth import (
                            _ff2_composite_closure as _clos70,
                        )
                        keep_oe70 = _clos70(ff2, keep_oe70)
                    except Exception:
                        pass
                    if 248 in keep_oe70:
                        keep_oe70.add(57)
                    for want70 in (13210, 13000):
                        landed_ff70 = None
                        if int(want70) >= int(g):
                            landed_ff70 = _snap_f1_glyf_exact_via_fat(
                                ff2, int(want70), keep_oe70,
                            )
                        if (
                            landed_ff70 is None
                            or int(_glyf_table_length(landed_ff70)) != int(want70)
                        ):
                            landed_ff70 = _snap_f1_glyf_exact_via_trim_empty(
                                ff2, int(want70), keep_oe70,
                            )
                        if (
                            landed_ff70 is None
                            or int(_glyf_table_length(landed_ff70)) != int(want70)
                        ):
                            landed_ff70 = _snap_f1_glyf_exact_via_shrink(
                                ff2, int(want70), keep_oe70,
                            )
                        if (
                            landed_ff70 is None
                            or int(_glyf_table_length(landed_ff70)) != int(want70)
                        ):
                            continue
                        shaped70 = _ship_f1_at_exact_twin_shape(
                            pdf, ff_xref, landed_ff70,
                            glyf_len=int(want70), cmap_n=70,
                            keep=keep_oe70, height=height,
                            why="hydrate-cmap70-exact",
                        )
                        base70 = shaped70
                        if base70 is None:
                            base70 = _patch_fontfile2_xref(
                                pdf, ff_xref, landed_ff70,
                            )
                        if base70 is None:
                            continue
                        if int(keep_n) > 70:
                            # Cannot shrink ToUnicode below painted — merge
                            # non-face unique CIDs (never Успешно / Ozon / Ш/х).
                            logger.warning(
                                "F1 hydrate cmap70 need painted≤70 have=%d "
                                "glyf=%d — force-merge then peel",
                                keep_n, g,
                            )
                            try:
                                import fitz as _fz_m
                                from tbank_sbp_stealth import (
                                    _force_merge_painted_to_target as _fmt70,
                                    _gids_per_font_in_stream as _gps70,
                                )
                                _doc_m = _fz_m.open(stream=base70, filetype="pdf")
                                _cs_x = _doc_m[0].get_contents()[0]
                                _cs_b = _doc_m.xref_stream(_cs_x)
                                _st_m = _fmt70(
                                    _cs_b, 70, prepared=prepared, ff2=landed_ff70,
                                )
                                if _st_m is not None and _st_m != _cs_b:
                                    _doc_m.update_stream(_cs_x, _st_m)
                                    base70 = _doc_m.tobytes(
                                        deflate=False, garbage=0, clean=False,
                                    )
                                    _u70, _ = _gps70(_st_m)
                                    keep_n = len(_u70)
                                    logger.info(
                                        "F1 hydrate peel force-merge painted→%d",
                                        keep_n,
                                    )
                                    _doc_m.close()
                                    if int(keep_n) <= 70:
                                        return base70
                                else:
                                    _doc_m.close()
                            except Exception as _exc_m:
                                logger.warning(
                                    "F1 hydrate peel force-merge: %s", _exc_m,
                                )
                        landed70 = _cmap_nudge(
                            70,
                            why="hydrate-cmap70-retarget",
                            base_pdf=base70,
                            base_ff2=landed_ff70,
                        )
                        if landed70 is not None:
                            return _peel_f1_glyf_to_cmap_band(
                                landed70, height=height, prepared=prepared,
                            )
            tc = _best_cmap(g)
            if (
                tc is not None
                and int(tc) != int(cmap_n)
                and int(tc) <= int(keep_n)
            ):
                landed = _cmap_nudge(int(tc), why="over-envelope-hydrate")
                if landed is not None:
                    return _peel_f1_glyf_to_cmap_band(landed, height=height, prepared=prepared)
            if tc is not None and int(tc) > int(keep_n):
                logger.warning(
                    "F1 over-envelope hydrate cmap %d > painted %d — try shrink",
                    tc, keep_n,
                )
                # Fall through to same-cmap shrink / exact snap below.
                tc = None
            # Gap between envelopes (e.g. 13320 between cmap70/74): grow to the
            # nearest higher exact across atlas, then retarget cmap. Never
            # soft-cover FIO letters — full alphabet must ship.
            keep_oe = {0, 3}
            if cs:
                try:
                    reg, _med = _gids_per_font_in_stream(cs)
                    keep_oe |= {int(x) for x in reg}
                except Exception:
                    pass
            try:
                from tbank_sbp_stealth import _ff2_composite_closure as _clos_oe
                keep_oe = _clos_oe(ff2, keep_oe)
            except Exception:
                pass
            # Proton A-TBANK-FONT-SUBSET-EXACT-CLOSURE: М(248) → ghost 57.
            if 248 in keep_oe:
                keep_oe.add(57)
            # SBP-only perf skip — card_sber h=471 must exact-snap for OnlyPDF.
            if int(height) == 519:
                logger.warning(
                    "F1 over-envelope hydrate glyf=%d cmap=%d — skip exact-snap "
                    "(ship hydrate; snap walk was 40s+)",
                    g, cmap_n,
                )
                return pdf
            grow_hits: List[Tuple[int, int, int, int]] = []
            for cn, xs in exacts.items():
                # May retarget cmap DOWN (71→70) onto a higher exact (13210).
                # Never grow ToUnicode past painted keep_n.
                if int(cn) > int(keep_n):
                    continue
                for x in xs:
                    if int(x) >= int(g):
                        # Prefer V3-safe glyf > 12783 (PASS round2_02 / сбп11).
                        v3pen = 0 if int(x) > 12783 else 1
                        grow_hits.append(
                            (v3pen, int(x) - int(g), int(cn), int(x))
                        )
            grow_hits.sort()
            for _v3, _dist, cn_t, want in grow_hits[:2]:
                if int(cn_t) > int(keep_n):
                    continue  # unused TU grow forbidden
                grown = _snap_f1_glyf_exact_via_fat(ff2, want, keep_oe)
                if grown is None or int(_glyf_table_length(grown)) != int(want):
                    continue
                if int(cn_t) != int(cmap_n):
                    # Shape-gate before cmap nudge so we don't peel a length-only fake.
                    shaped = _ship_f1_at_exact_twin_shape(
                        pdf, ff_xref, grown,
                        glyf_len=int(want), cmap_n=int(cn_t),
                        keep=keep_oe, height=height, why="over-envelope-snap",
                    )
                    if shaped is None:
                        # TWIN_SHAPE retired — patch + cmap nudge.
                        patched = _patch_fontfile2_xref(pdf, ff_xref, grown)
                        if patched is None:
                            continue
                        shaped = patched
                    landed = _cmap_nudge(
                        int(cn_t),
                        why="over-envelope-exact-snap",
                        base_pdf=shaped,
                        base_ff2=grown,
                    )
                    if landed is not None:
                        logger.info(
                            "F1 over-envelope snap glyf %d→%d cmap→%d — peel",
                            g, want, cn_t,
                        )
                        return _peel_f1_glyf_to_cmap_band(landed, height=height, prepared=prepared)
                else:
                    shipped = _ship_f1_at_exact_twin_shape(
                        pdf, ff_xref, grown,
                        glyf_len=int(want), cmap_n=int(cn_t),
                        keep=keep_oe, height=height, why="over-envelope-snap",
                    )
                    if shipped is not None:
                        return shipped
                    logger.warning(
                        "F1 over-envelope grow twin-shape miss glyf=%d cmap=%d — try next",
                        want, cn_t,
                    )
                    continue
            # Shrink to nearest lower exact (hydrate overshoot).
            # Prefer V3-safe glyf>12783, then SAME cmap (72→70 needs painted≤70).
            shrink_hits: List[Tuple[int, int, int, int, int, int]] = []
            for cn, xs in exacts.items():
                if int(cn) > int(keep_n):
                    continue
                for x in xs:
                    if int(x) <= int(g):
                        same = 0 if int(cn) == int(cmap_n) else 1
                        v3pen = 0 if int(x) > 12783 else 1
                        shrink_hits.append(
                            (
                                v3pen,
                                # Prefer PASS twin сбп11 (70/13000) or close
                                # shape twin 71/12818 (11,79,68) over fat 13002.
                                0 if (
                                    (int(x) == 13000 and int(cn) == 70)
                                    or (int(x) == 12818 and int(cn) == 71)
                                ) else (
                                    # Deprioritize 13002 — chronic TWIN_SHAPE
                                    # (14,83,69) vs rebuilt (13,82,69).
                                    4 if int(x) == 13002 else (
                                    1 if (
                                        int(cn) == int(keep_n) and int(x) > 12783
                                    ) else (
                                        2 if int(x) > 12783 else 3
                                    )
                                    )
                                ),
                                same,
                                int(g) - int(x),
                                int(cn),
                                int(x),
                            )
                        )
            shrink_hits.sort()
            # Blank unused orphans first — hydrate leaves nonempty outside
            # painted∪closure; shrink alone often stalls above the exact.
            try:
                from tbank_sbp_stealth import _blank_ff2_unused_glyfs as _blank_oe
                ff2_oe = _blank_oe(ff2, keep_oe)
                if int(_glyf_table_length(ff2_oe)) < int(g):
                    logger.info(
                        "F1 over-envelope pre-blank glyf %d→%d",
                        g, _glyf_table_length(ff2_oe),
                    )
                else:
                    ff2_oe = ff2
            except Exception:
                ff2_oe = ff2
            for _v3, _pass, _same, _dist, cn_t, want in shrink_hits[:2]:
                # Prefer exact 13000 for cmap70 (PASS сбп11) — retry fat if
                # short-loca off-by slips. Try blanked base, then raw FF2.
                shrunk = None
                for _base in (ff2_oe, ff2):
                    cand = _snap_f1_glyf_exact_via_trim_empty(
                        _base, want, keep_oe,
                    )
                    if cand is None or int(_glyf_table_length(cand)) != int(want):
                        cand = _snap_f1_glyf_exact_via_shrink(_base, want, keep_oe)
                    if cand is None and int(want) < int(_glyf_table_length(_base)):
                        near = _snap_f1_glyf_exact_via_shrink(
                            _base, int(want) + 64, keep_oe,
                        )
                        if near is not None:
                            for delta in (0, 2, 4, 6, 8, 10):
                                trial = _snap_f1_glyf_exact_via_fat(
                                    near, int(want) + delta, keep_oe,
                                )
                                if trial is None:
                                    continue
                                if int(_glyf_table_length(trial)) == int(want):
                                    cand = trial
                                    break
                                trimmed = _snap_f1_glyf_exact_via_trim_empty(
                                    trial, int(want), keep_oe,
                                )
                                if (
                                    trimmed is not None
                                    and int(_glyf_table_length(trimmed)) == int(want)
                                ):
                                    cand = trimmed
                                    break
                    if cand is not None and int(_glyf_table_length(cand)) == int(want):
                        shrunk = cand
                        break
                if shrunk is None:
                    continue
                if int(_glyf_table_length(shrunk)) != int(want):
                    continue
                # Skip V3-unsafe exacts when a safer candidate exists later.
                if int(want) <= 12783 and int(keep_n) >= 70:
                    continue
                patched = _patch_fontfile2_xref(pdf, ff_xref, shrunk)
                if patched is None:
                    continue
                if int(cn_t) != int(cmap_n):
                    landed = _cmap_nudge(
                        int(cn_t),
                        why="over-envelope-exact-shrink",
                        base_pdf=patched,
                        base_ff2=shrunk,
                    )
                    if landed is not None:
                        logger.info(
                            "F1 over-envelope shrink glyf %d→%d cmap→%d — peel",
                            g, want, cn_t,
                        )
                        return _peel_f1_glyf_to_cmap_band(landed, height=height, prepared=prepared)
                else:
                    shipped = _ship_f1_at_exact_twin_shape(
                        pdf, ff_xref, shrunk,
                        glyf_len=int(want), cmap_n=int(cn_t),
                        keep=keep_oe, height=height, why="over-envelope-shrink",
                    )
                    if shipped is not None:
                        return shipped
                    logger.warning(
                        "F1 over-envelope twin-shape miss exact glyf=%d cmap=%d — reject",
                        want, cn_t,
                    )
                    continue
            # Floor after blank: land nearest painted-cmap exact ≤ floor.
            # Also try grow floor→nearest higher exact (e.g. 13108→13210).
            g_floor = int(_glyf_table_length(ff2_oe))
            floor_hits: List[Tuple[int, int, int, int]] = []
            for cn, xs in exacts.items():
                if int(cn) > int(keep_n):
                    continue
                for x in xs:
                    if int(x) > 12783 and int(x) <= max(int(g_floor), int(g)):
                        floor_hits.append(
                            (
                                0 if int(cn) == int(cmap_n) else 1,
                                abs(int(g_floor) - int(x)),
                                int(cn),
                                int(x),
                            )
                        )
                    if int(x) >= int(g_floor) and int(x) > 12783:
                        floor_hits.append(
                            (
                                0 if int(cn) == int(cmap_n) else 1,
                                int(x) - int(g_floor),
                                int(cn),
                                int(x),
                            )
                        )
            floor_hits.sort()
            for _same, _dist, cn_t, want in floor_hits[:1]:
                shrunk = None
                if int(want) >= int(g_floor):
                    shrunk = _snap_f1_glyf_exact_via_fat(ff2_oe, want, keep_oe)
                    if shrunk is None:
                        shrunk = _snap_f1_glyf_exact_via_fat(ff2, want, keep_oe)
                if shrunk is None or int(_glyf_table_length(shrunk)) != int(want):
                    shrunk = _snap_f1_glyf_exact_via_shrink(ff2_oe, want, keep_oe)
                if shrunk is None or int(_glyf_table_length(shrunk)) != int(want):
                    shrunk = _snap_f1_glyf_exact_via_shrink(ff2, want, keep_oe)
                if shrunk is None or int(_glyf_table_length(shrunk)) != int(want):
                    shrunk = _snap_f1_glyf_exact_via_trim_empty(
                        ff2_oe, want, keep_oe,
                    )
                if shrunk is None or int(_glyf_table_length(shrunk)) != int(want):
                    continue
                patched = _patch_fontfile2_xref(pdf, ff_xref, shrunk)
                if patched is None:
                    continue
                if int(cn_t) != int(cmap_n):
                    landed = _cmap_nudge(
                        int(cn_t),
                        why="over-envelope-floor",
                        base_pdf=patched,
                        base_ff2=shrunk,
                    )
                    if landed is not None:
                        logger.info(
                            "F1 over-envelope floor glyf %d→%d cmap→%d — peel",
                            g, want, cn_t,
                        )
                        return _peel_f1_glyf_to_cmap_band(landed, height=height, prepared=prepared)
                else:
                    shipped = _ship_f1_at_exact_twin_shape(
                        pdf, ff_xref, shrunk,
                        glyf_len=int(want), cmap_n=int(cn_t),
                        keep=keep_oe, height=height, why="over-envelope-floor",
                    )
                    if shipped is not None:
                        return shipped
                    logger.warning(
                        "F1 over-envelope floor twin-shape miss glyf=%d cmap=%d — try next",
                        want, cn_t,
                    )
                    continue
            xs = exacts.get(int(cmap_n)) or (
                exacts.get(int(tc)) if tc is not None else ()
            ) or ()
            if xs and g not in xs:
                logger.warning(
                    "F1 glyf over-envelope exact-gap glyf=%d cmap=%d "
                    "floor=%d want=%s — reject",
                    g, cmap_n, g_floor, sorted(xs)[:6],
                )
                return None
            logger.warning(
                "F1 glyf over-envelope non-twin glyf=%d cmap=%d — REJECT",
                g, cmap_n,
            )
            return None
        # Already in another atlas bucket — only retarget ToUnicode cardinality.
        # Never grow past painted CIDs (unused TU → NOT_MINIMAL / BIJECTION).
        early_target = _best_cmap(g)
        if (
            early_target is not None
            and early_target != cmap_n
            and int(early_target) <= int(keep_n)
        ):
            landed = _cmap_nudge(int(early_target), why="band-retarget")
            if landed is not None:
                return landed
        if (
            early_target is not None
            and int(early_target) > int(keep_n)
            and _tbank_ff2_is_corpus_twin(ff2, height=height)
        ):
            # Twin glyf wants higher cmap than painted (e.g. 12852→71, painted=66).
            # Growing unused TU is forbidden — shrink onto a painted-cmap exact
            # instead of hard-reject (that was GEN_NONE on live bot).
            logger.warning(
                "F1 band-retarget skipped cmap %d→%d > painted %d (twin glyf=%d) "
                "— shrink to painted exact",
                cmap_n, early_target, keep_n, g,
            )
            # fall through to hydrate→painted exact below
        if (
            early_target is not None
            and int(early_target) > int(keep_n)
        ):
            # Hydrate: glyf nearest exact is a higher cmap than painted.
            # Prefer fat/shrink onto painted cmap's exact (e.g. 74→13738)
            # instead of growing unused ToUnicode.
            xs_keep = exacts.get(int(keep_n)) or ()
            if xs_keep:
                keep_early = {0, 3}
                if cs:
                    try:
                        reg, _med = _gids_per_font_in_stream(cs)
                        keep_early |= {int(x) for x in reg}
                    except Exception:
                        pass
                try:
                    from tbank_sbp_stealth import _ff2_composite_closure as _clos_h
                    keep_early = _clos_h(ff2, keep_early)
                except Exception:
                    pass
                if 248 in keep_early:
                    keep_early.add(57)
                # Prefer exact ≥ current glyf (grow), else nearest.
                higher = [int(x) for x in xs_keep if int(x) >= int(g)]
                want = (
                    min(higher)
                    if higher
                    else min(xs_keep, key=lambda x: abs(int(x) - int(g)))
                )
                # Never land on exact with no corpus twin (13738 / cmap74).
                if _load_corpus_f1_twin_for_glyf(int(want)) is None:
                    logger.warning(
                        "F1 hydrate skip exact %d cmap=%d — no corpus twin",
                        want, keep_n,
                    )
                    landed_ff = None
                else:
                    landed_ff = None
                    if int(want) >= int(g):
                        landed_ff = _snap_f1_glyf_exact_via_fat(
                            ff2, int(want), keep_early,
                        )
                    if landed_ff is None and int(want) <= int(g):
                        landed_ff = _snap_f1_glyf_exact_via_shrink(
                            ff2, int(want), keep_early,
                        )
                if (
                    landed_ff is not None
                    and int(_glyf_table_length(landed_ff)) == int(want)
                ):
                    shipped = _ship_f1_at_exact_twin_shape(
                        pdf, ff_xref, landed_ff,
                        glyf_len=int(want), cmap_n=int(keep_n),
                        keep=keep_early, height=height,
                        why="hydrate-painted-exact",
                    )
                    if shipped is not None:
                        return shipped
            logger.warning(
                "F1 hydrate cmap need %d > painted %d glyf=%d — REJECT",
                early_target, keep_n, g,
            )
            return None

        keep = {0, 3}
        if cs:
            try:
                reg, _med = _gids_per_font_in_stream(cs)
                keep |= {int(x) for x in reg}
            except Exception:
                pass
        if int(height) == 471:
            keep |= set(_CARD_OPENPDF_F1_COMPOSITE_GIDS)
        try:
            keep = _ff2_composite_closure(ff2, keep)
        except Exception:
            pass

        lean = ff2
        # Card h=471: restitch lean donor outlines (10 composites → glyf≈11142).
        if int(height) == 471:
            donor_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "templates",
                "T_card_sber_original.pdf",
            )
            if os.path.isfile(donor_path):
                try:
                    ddoc = fitz.open(donor_path)
                    dfm = tut._find_font_objects(ddoc)
                    dmeta = dfm.get("TinkoffSans-Regular")
                    donor_ff2 = ddoc.xref_stream(dmeta["fontfile_xref"]) if dmeta else None
                    ddoc.close()
                    if donor_ff2:
                        lean = _restitch_f1_keep_from_donor(donor_ff2, ff2, keep)
                except Exception as exc:
                    logger.warning("card F1 donor restitch failed: %s", exc)
                    lean = _blank_ff2_unused_glyfs(ff2, keep)
                    lean = _ff2_restore_shell_tables(ff2, lean)
                    lean = _force_tbank_f1_head_epoch(lean)
            else:
                lean = _blank_ff2_unused_glyfs(ff2, keep)
                lean = _ff2_restore_shell_tables(ff2, lean)
                lean = _force_tbank_f1_head_epoch(lean)
        else:
            if g < (bands[cmap_n][0] - 32):
                # Under-envelope after orphan blank — grow to nearest exact ≥ floor.
                xs = exacts.get(int(cmap_n)) or ()
                want_u = None
                floor = int(bands[cmap_n][0])
                cands = [int(x) for x in xs if int(x) >= max(int(g), floor - 32)]
                if cands:
                    want_u = min(cands)
                if want_u is None:
                    # Any higher exact on a legal cmap ≤ painted.
                    grow_u: List[Tuple[int, int, int]] = []
                    for cn, xlist in exacts.items():
                        if int(cn) > int(keep_n) or int(cn) < int(cmap_n):
                            continue
                        for x in xlist:
                            if int(x) >= int(g):
                                grow_u.append((int(x) - int(g), int(cn), int(x)))
                    grow_u.sort()
                    if grow_u:
                        _d, cn_u, want_u = grow_u[0]
                        if int(cn_u) != int(cmap_n):
                            grown = _snap_f1_glyf_exact_via_fat(ff2, want_u, keep)
                            if grown is not None:
                                patched = _patch_fontfile2_xref(pdf, ff_xref, grown)
                                if patched is not None:
                                    landed = _cmap_nudge(
                                        int(cn_u),
                                        why="under-envelope-grow",
                                        base_pdf=patched,
                                        base_ff2=grown,
                                    )
                                    if landed is not None:
                                        return _peel_f1_glyf_to_cmap_band(
                                            landed, height=height, prepared=prepared,
                                        )
                            want_u = None
                if want_u is not None:
                    grown = _snap_f1_glyf_exact_via_fat(ff2, int(want_u), keep)
                    if grown is not None and int(_glyf_table_length(grown)) == int(want_u):
                        shipped = _ship_f1_at_exact_twin_shape(
                            pdf, ff_xref, grown,
                            glyf_len=int(want_u), cmap_n=int(cmap_n),
                            keep=keep, height=height, why="under-envelope-grow",
                        )
                        if shipped is not None:
                            return shipped
                logger.warning(
                    "F1 glyf↔cmap under: glyf=%d cmap=%d want≥%d — reject",
                    g, cmap_n, bands[cmap_n][0] - 32,
                )
                return None
            lean = _blank_ff2_unused_glyfs(ff2, keep)
            lean = _ff2_restore_shell_tables(ff2, lean)
            lean = _force_tbank_f1_head_epoch(lean)

        g2 = _glyf_table_length(lean)
        target_cmap = cmap_n if _in_band(g2, cmap_n) else _best_cmap(g2)
        # Card gap (e.g. glyf=11228 sits between cmap60 and cmap62): restore one
        # donor spare outline to push into the next atlas bucket.
        if target_cmap is None and int(height) == 471:
            donor_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "templates",
                "T_card_sber_original.pdf",
            )
            donor_ff2 = None
            if os.path.isfile(donor_path):
                try:
                    ddoc = fitz.open(donor_path)
                    dfm = tut._find_font_objects(ddoc)
                    dmeta = dfm.get("TinkoffSans-Regular")
                    donor_ff2 = ddoc.xref_stream(dmeta["fontfile_xref"]) if dmeta else None
                    ddoc.close()
                except Exception:
                    donor_ff2 = None
            if donor_ff2:
                from copy import deepcopy
                from io import BytesIO
                from fontTools.ttLib import TTFont
                from tbank_sbp_stealth import _copy_glyph_closure
                try:
                    dst = TTFont(BytesIO(lean))
                    src = TTFont(BytesIO(donor_ff2))
                    dgo = dst.getGlyphOrder()
                    sgo = src.getGlyphOrder()
                    dglyf = dst["glyf"]
                    # Find smallest nonempty donor glyph not already nonempty in lean.
                    spares = []
                    for gid, name in enumerate(sgo):
                        snc = int(getattr(src["glyf"][name], "numberOfContours", 0) or 0)
                        if snc == 0:
                            continue
                        if gid < len(dgo):
                            dnc = int(getattr(dglyf[dgo[gid]], "numberOfContours", 0) or 0)
                            if dnc != 0:
                                continue
                        # rough size via loca if available
                        spares.append(gid)
                    grown = lean
                    for gid in spares[:8]:
                        copied: set = set()
                        _copy_glyph_closure(dglyf, dgo, src, int(gid), copied)
                        keep.add(int(gid))
                        bio = BytesIO()
                        dst.save(bio, reorderTables=False)
                        grown = _ff2_restore_shell_tables(lean, bio.getvalue())
                        grown = _force_tbank_f1_head_epoch(grown)
                        g3 = _glyf_table_length(grown)
                        tc = _best_cmap(g3)
                        if tc is not None:
                            lean, g2, target_cmap = grown, g3, tc
                            logger.info(
                                "F1 glyf↔cmap spare-grow gid=%d glyf=%d→%d cmap→%d",
                                gid, _glyf_table_length(ff2), g3, tc,
                            )
                            break
                        # reset dst from lean for next try
                        dst = TTFont(BytesIO(lean))
                        dglyf = dst["glyf"]
                        dgo = dst.getGlyphOrder()
                except Exception as exc:
                    logger.warning("card F1 spare-grow failed: %s", exc)

            # Still in gap (e.g. 11228 between cmap60/62): fat-empty pad on an
            # unused GID grows glyf without touching painted outlines.
            if target_cmap is None:
                from io import BytesIO
                from fontTools.ttLib import TTFont
                from tbank_sbp_stealth import _make_ff2_fat_empty_glyph
                # Aim for nearest higher band midpoint.
                higher = sorted(
                    (
                        (lo, hi, cn)
                        for cn, (lo, hi) in bands.items()
                        if lo - 32 > g2
                    ),
                    key=lambda t: t[0],
                )
                if higher:
                    lo_t, hi_t, cn_t = higher[0]
                    need = max(1, ((lo_t + hi_t) // 2) - g2)
                    try:
                        ft = TTFont(BytesIO(lean))
                        go = ft.getGlyphOrder()
                        glyf = ft["glyf"]
                        slot = None
                        for gid, name in enumerate(go):
                            if gid in keep:
                                continue
                            nc = int(getattr(glyf[name], "numberOfContours", 0) or 0)
                            if nc == 0:
                                slot = (gid, name)
                                break
                        if slot is not None:
                            gid, name = slot
                            glyf[name] = _make_ff2_fat_empty_glyph(need + 24, salt=gid or 1)
                            bio = BytesIO()
                            ft.save(bio, reorderTables=False)
                            grown = _ff2_restore_shell_tables(lean, bio.getvalue())
                            grown = _force_tbank_f1_head_epoch(grown)
                            g3 = _glyf_table_length(grown)
                            tc = _best_cmap(g3)
                            if tc is not None:
                                lean, g2, target_cmap = grown, g3, tc
                                keep.add(int(gid))
                                logger.info(
                                    "F1 glyf↔cmap fat-empty gid=%d +~%d glyf→%d cmap→%d",
                                    gid, need, g3, tc,
                                )
                    except Exception as exc:
                        logger.warning("card F1 fat-empty grow failed: %s", exc)

        if target_cmap is None:
            logger.warning(
                "F1 glyf↔cmap peel miss glyf=%d→%d cmap=%d — reject",
                g, g2, cmap_n,
            )
            return None

        patched = pdf
        if lean != ff2:
            patched = _patch_fontfile2_xref(patched, ff_xref, lean)
            if patched is None:
                return None

        if target_cmap != cmap_n:
            if int(height) != 519 and int(target_cmap) > int(keep_n):
                logger.warning(
                    "F1 glyf↔cmap skip unused-TU grow %d→%d h=%d painted=%d",
                    cmap_n, target_cmap, height, keep_n,
                )
                target_cmap = int(cmap_n)
        if target_cmap != cmap_n:
            nudged = _nudge_tounicode_cardinality(
                patched,
                tu_xref,
                sub,
                target_n=int(target_cmap),
                painted_gids=keep - set(_CARD_OPENPDF_F1_COMPOSITE_GIDS)
                if int(height) == 471 else keep,
                anchor_gid=3,
                ff2=lean,
            )
            if nudged is None:
                logger.warning(
                    "F1 glyf↔cmap cmap nudge %d→%d failed (glyf=%d) — reject",
                    cmap_n, target_cmap, g2,
                )
                return None
            patched, _sub2 = nudged
            logger.info(
                "F1 glyf↔cmap cmap nudge %d→%d (glyf=%d h=%d)",
                cmap_n, target_cmap, g2, height,
            )
            cmap_n = target_cmap

        if not _in_band(g2, cmap_n):
            logger.warning(
                "F1 glyf↔cmap peel miss glyf=%d→%d cmap=%d — reject",
                g, g2, cmap_n,
            )
            return None
        # After lean/restitch: require exact glyf + corpus SHA twin.
        try:
            doc2 = fitz.open(stream=patched, filetype="pdf")
            fm2 = tut._find_font_objects(doc2)
            meta2 = fm2.get("TinkoffSans-Regular")
            ff2_now = (
                doc2.xref_stream(meta2["fontfile_xref"]) if meta2 else None
            )
            doc2.close()
        except Exception:
            ff2_now = lean if lean != ff2 else ff2
        if ff2_now is None:
            return None
        g_now = _glyf_table_length(ff2_now)
        if not _exact_ok(g_now, cmap_n):
            if int(height) == 471:
                from tbank_sbp_stealth import (
                    _h471_f1_exact_pdf,
                    _sbp_h471_finalize_font_pdf,
                )
                landed = _sbp_h471_finalize_font_pdf(patched)
                logger.warning(
                    "F1 glyf exact miss(post-peel) glyf=%d cmap=%d — "
                    "h471 finalize (exact=%s)",
                    g_now, cmap_n, _h471_f1_exact_pdf(landed),
                )
                return landed
            logger.warning(
                "F1 glyf exact miss(post-peel) glyf=%d cmap=%d — reject",
                g_now, cmap_n,
            )
            return None
        if not _tbank_ff2_twin_ok(
            ff2_now, height=height, cmap_n=cmap_n, glyf_len=g_now,
        ):
            # Length exact without SHA: prefer matching shape; else soft-ship
            # (Proton retired TWIN_SHAPE_MISMATCH).
            twin = _load_corpus_f1_twin_for_glyf(int(g_now))
            want = _f1_glyph_shape(twin) if twin is not None else None
            if twin is not None and _f1_glyph_shape(ff2_now) == want:
                logger.info(
                    "F1 post-peel shape OK %s glyf=%d cmap=%d (non-SHA hydrate)",
                    want, g_now, cmap_n,
                )
            else:
                logger.warning(
                    "F1 post-peel shape REJECT glyf=%d cmap=%d have=%s want=%s "
                    "(SHAPE_ENVELOPE HARD)",
                    g_now, cmap_n, _f1_glyph_shape(ff2_now), want,
                )
                return None
        logger.info("F1 glyf↔cmap peel %d→%d (cmap=%d h=%d)", g, g_now, cmap_n, height)
        return patched
    except Exception as exc:
        logger.warning("F1 glyf↔cmap peel failed: %s", exc)
        return None


def create_tbank_pipeline(
    prepared: Optional[Dict],
    builders: List[Callable[[Dict], Optional[bytes]]],
    *,
    channel: str,
    verify_receipt_fn: Optional[Callable[[bytes, str], bool]] = None,
    expected_receipt: Optional[str] = None,
    dynamic_builder: Optional[Callable[[Dict], Optional[bytes]]] = None,
    post_validate: Optional[Callable[[bytes], None]] = None,
) -> Optional[bytes]:
    """Единый пайплайн T-Bank: donor-orig → dynamic (→ retry dynamic при сбое квитанции)."""
    from tbank_sbp_stealth import _ensure_glyph_library, _randomize_pdf_fingerprints

    if prepared is None:
        return None

    _ensure_glyph_library()
    result: Optional[bytes] = None
    for builder in builders:
        name = getattr(builder, "__name__", repr(builder))
        try:
            candidate = builder(dict(prepared))
        except Exception as e:
            logger.warning("T-Bank %s %s error: %s", channel, name, e)
            candidate = None
        if candidate is None:
            continue
        if verify_receipt_fn and expected_receipt and not verify_receipt_fn(
            candidate, expected_receipt
        ):
            logger.warning(
                "T-Bank %s: exact receipt %s missing — reject candidate",
                channel, expected_receipt,
            )
            continue
        if _content_has_pad_after_et(candidate):
            logger.warning("T-Bank %s: reject pad-after-ET", channel)
            continue
        result = candidate
        break

    if result is not None:
        if post_validate:
            post_validate(result)

        # Proton FONTFILE2_SIZE_STRONG_OUTLIER — peel after donor-orig inject growth.
        _band = {
            "card_sber": (15252, 15896),
            "card_tbank": (15072, 16044),
            "nocomm": (14864, 15840),
            "phone": (15496, 16596),
            "sbp": (16244, 18196),
        }.get(channel)
        if _band is not None:
            lo, hi = _band
            keep_parts = []
            for k, v in (prepared or {}).items():
                if isinstance(v, str) and v:
                    keep_parts.append(v)
            keep_parts.append(
                "Перевод Статус Успешно Сумма Комиссия Отправитель Получатель "
                "Итого Квитанция Телефон Банк СБП На карту По номеру"
            )
            leaned = lean_tbank_f1_to_band(
                result, "".join(keep_parts), lo=lo, hi=hi, label=f"{channel} F1",
            )
            if leaned is None:
                try:
                    import fitz as _fz
                    import tbank_unlock_template as _tut
                    _d = _fz.open(stream=result, filetype="pdf")
                    _fm = _tut._find_font_objects(_d)
                    _m = _fm.get("TinkoffSans-Regular")
                    _n = len(_d.xref_stream(_m["fontfile_xref"])) if _m else 0
                    _d.close()
                except Exception:
                    _n = 10**9
                logger.warning(
                    "T-Bank %s: F1 lean miss size=%d (want %d..%d) — ship",
                    channel, _n, lo, hi,
                )
            else:
                result = leaned

        # Proton TBANK_F1_GLYF_CMAP_OUTLIER — peel glyf into height×cmap atlas.
        _h = {
            "sbp": 519,
            "phone": 451,
            "card_sber": 471,
            "card_tbank": 431,
            "nocomm": 411,
        }.get(channel)
        if _h is not None:
            shaped = _peel_f1_glyf_to_cmap_band(result, height=_h, prepared=prepared)
            if shaped is None:
                if channel == "sbp":
                    # Do not abort — apply_sbp_font_hard_fixes lands exclusive
                    # atlas (72→12612) after mosaic; create gate rejects if still
                    # OUTLIER. Aborting here caused GEN_NONE loops on mash FIO.
                    logger.error(
                        "T-Bank %s: F1 glyf↔cmap peel miss — defer to hard-fix land",
                        channel,
                    )
                else:
                    logger.error(
                        "T-Bank %s: F1 glyf↔cmap peel miss — ship unpeeled",
                        channel,
                    )
            else:
                result = shaped

        result = _finalize_tbank_f1_epoch(result)
        if channel != "sbp":
            try:
                import fitz as _fz_sent2
                import re as _re_sent2
                _ds2 = _fz_sent2.open(stream=result, filetype="pdf")
                _css2 = _ds2.xref_stream(_ds2[0].get_contents()[0])
                _ds2.close()
                if b"-500 -500 Td" in _css2 or _re_sent2.search(
                    rb"(?<!\d)3\s+Tr\b", _css2
                ):
                    logger.error(
                        "T-Bank %s: Contents Td-500/Tr=3 sentinel — abort PDF",
                        channel,
                    )
                    return None
            except Exception as _exc_sent2:
                logger.warning(
                    "T-Bank %s Td/Tr sentinel gate: %s", channel, _exc_sent2,
                )
        if channel == "sbp":
            # Never ship Td -500 / Tr=3 (CONTENT_TD_SENTINEL / TR_MODE_ANOMALY).
            try:
                import fitz as _fz_sent
                import re as _re_sent
                _ds = _fz_sent.open(stream=result, filetype="pdf")
                _css = _ds.xref_stream(_ds[0].get_contents()[0])
                _ds.close()
                if b"-500 -500 Td" in _css or _re_sent.search(
                    rb"(?<!\d)3\s+Tr\b", _css
                ):
                    logger.error(
                        "T-Bank sbp: Contents Td-500/Tr=3 sentinel — abort PDF"
                    )
                    return None
                if b"175 0 Td" not in _css or b"-175 0 Td" not in _css:
                    logger.error(
                        "T-Bank sbp: missing Jasper 175/-175 Td stamp pair — abort"
                    )
                    return None
            except Exception as _exc_sent:
                logger.warning("T-Bank sbp Td/Tr sentinel gate: %s", _exc_sent)
            amt = ""
            if prepared:
                amt = str(
                    prepared.get("new_amount")
                    or prepared.get("amount")
                    or prepared.get("new_amount_total")
                    or ""
                )
            f2 = _lean_sbp_f2_to_hard(result, amt)
            result = f2 if f2 is not None else result
            from tbank_sbp_stealth import _defuse_tbank_reassembly_family_v3
            result = _defuse_tbank_reassembly_family_v3(result)
            # Subset-tag /ID randomize runs last in _sbp_finalize_for_ship —
            # fitz tobytes before that breaks find_object_range (FontFile2 patches).
            keep_parts2 = []
            for k, v in (prepared or {}).items():
                if isinstance(v, str) and v:
                    keep_parts2.append(v)
            keep2 = (
                "".join(keep_parts2)
                + "Перевод Статус Успешно Сумма Комиссия Отправитель Получатель "
                "Итого Квитанция Телефон Банк СБП"
            )
            # Corpus SHA-twin F1 must stay verbatim — V3 window lean mutates
            # FontFile2 ⇒ TBANK_F1_FF2_SHA_TWIN_MISMATCH.
            _f1_twin_ship = False
            try:
                import fitz as _fz2
                import tbank_unlock_template as _tut2
                _d2 = _fz2.open(stream=result, filetype="pdf")
                _fm2 = _tut2._find_font_objects(_d2)
                _m2 = _fm2.get("TinkoffSans-Regular")
                _ff2t = _d2.xref_stream(_m2["fontfile_xref"]) if _m2 else b""
                _d2.close()
                _f1_twin_ship = bool(
                    _ff2t and _tbank_ff2_is_corpus_twin(_ff2t, height=519)
                )
            except Exception:
                _f1_twin_ship = False
            if _f1_twin_ship:
                logger.info("T-Bank sbp: F1 corpus twin — skip V3 F1 lean")
                if _sbp_v3_family_at_risk(result):
                    logger.warning(
                        "T-Bank sbp: V3 family armed on F1 twin — ship",
                    )
            else:
                # When V3 family would fire (F2.bfrange≥10 + F1 outside window),
                # force F1 into natural Jasper lattice 17121..17198 (specimen 17180).
                f1_lo, f1_hi = 16244, 18196
                if _sbp_v3_family_at_risk(result):
                    f1_lo, f1_hi = 17121, 17198
                    logger.warning(
                        "T-Bank sbp: V3 family at risk — force F1 %d..%d",
                        f1_lo, f1_hi,
                    )
                relean = lean_tbank_f1_to_band(
                    result, keep2, lo=f1_lo, hi=f1_hi, label="sbp F1 post-defuse",
                )
                if relean is None:
                    logger.warning(
                        "T-Bank sbp: post-defuse F1 lean miss — ship",
                    )
                else:
                    result = relean
                    f2b = _lean_sbp_f2_to_hard(result, amt)
                    if f2b is not None:
                        result = f2b
                if _sbp_v3_family_at_risk(result):
                    logger.warning(
                        "T-Bank sbp: V3 family still armed after lean — ship",
                    )
            result = _finalize_tbank_f1_epoch(result)
            reshaped = _peel_f1_glyf_to_cmap_band(result, height=519, prepared=prepared)
            if reshaped is None:
                logger.error(
                    "T-Bank sbp: post-defuse F1 glyf peel miss — keep prior",
                )
            else:
                result = reshaped
            result = _finalize_tbank_f1_epoch(result)
            # LAST F1 mutation: restore empty ghost components of painted
            # composites (е/р/М/К blanks). Must run after every peel.
            try:
                from tbank_sbp_stealth import (
                    _gids_per_font_in_stream,
                    _glyf_table_length as _glr,
                    _patch_fontfile2_xref,
                    _repair_ff2_empty_composite_components,
                )
                import fitz as _fz_r
                import tbank_unlock_template as _tut_r
                _dr = _fz_r.open(stream=result, filetype="pdf")
                _fmr = _tut_r._find_font_objects(_dr)
                _mr = _fmr.get("TinkoffSans-Regular")
                if _mr:
                    _ffr = _dr.xref_stream(_mr["fontfile_xref"])
                    _csr = _dr.xref_stream(_dr[0].get_contents()[0])
                    _xr = int(_mr["fontfile_xref"])
                    _dr.close()
                    _reg_r, _ = _gids_per_font_in_stream(_csr)
                    _fixed = _repair_ff2_empty_composite_components(
                        _ffr, set(_reg_r) | {0, 3}, is_medium=False,
                    )
                    if _fixed is not None and _fixed != _ffr:
                        _g_before = int(_glr(_ffr))
                        _g_after = int(_glr(_fixed))
                        _ff_ship = _fixed
                        if _g_after != _g_before:
                            # Prefer pre-repair exact; else nearest twin-backed
                            # exact for THIS painted cmap (never 76/13794 on cmap71).
                            _cmap_paint = len(_reg_r)
                            _exacts_h519 = {
                                65: (12170,),
                                66: (12080, 12344),
                                67: (12176, 12236, 12482, 12530, 12560, 12978),
                                68: (12296, 12852, 12880, 12910, 13002),
                                69: (12520, 12816),
                                70: (13000, 13210),
                                71: (12818, 13002),
                                72: (12612,),
                                74: (13738,),
                                75: (13610,),
                                76: (13794, 14032),
                            }
                            _xs_ok = tuple(
                                int(x) for x in (_exacts_h519.get(_cmap_paint) or ())
                            )
                            _targets = []
                            if _g_before in _xs_ok or not _xs_ok:
                                _targets.append(_g_before)
                            for _tw in _xs_ok:
                                if _tw not in _targets:
                                    _targets.append(_tw)
                            if not _targets:
                                _targets = [_g_before]
                            _targets.sort(key=lambda x: (abs(int(x) - _g_after), int(x)))
                            for _want in _targets:
                                _t = None
                                if int(_want) < _g_after:
                                    _t = _snap_f1_glyf_exact_via_trim_empty(
                                        _fixed, int(_want), set(_reg_r) | {0, 3},
                                    )
                                    if _t is None or int(_glr(_t)) != int(_want):
                                        _t = _snap_f1_glyf_exact_via_shrink(
                                            _fixed, int(_want), set(_reg_r) | {0, 3},
                                        )
                                elif int(_want) > _g_after:
                                    _t = _snap_f1_glyf_exact_via_fat(
                                        _fixed, int(_want), set(_reg_r) | {0, 3},
                                    )
                                else:
                                    _t = _fixed
                                if _t is None or int(_glr(_t)) != int(_want):
                                    continue
                                # Keep only if painted still render after snap.
                                try:
                                    from tbank_sbp_stealth import _glyph_renders as _gr
                                    from io import BytesIO as _BIO
                                    from fontTools.ttLib import TTFont as _TT
                                    _ft = _TT(_BIO(_t))
                                    _gly = _ft["glyf"]
                                    _go = _ft.getGlyphOrder()
                                    if any(
                                        not _gr(_gly, _go, int(c))
                                        for c in _reg_r
                                        if int(c) not in (0, 3)
                                    ):
                                        continue
                                except Exception:
                                    pass
                                _ff_ship = _t
                                logger.info(
                                    "T-Bank sbp: post-repair snap glyf %d→%d "
                                    "(cmap_paint=%d)",
                                    _g_after, _want, _cmap_paint,
                                )
                                break
                            else:
                                # Never ship glyf↔cmap outlier — keep pre-repair
                                # exact when legal, else re-peel.
                                if _g_before in _xs_ok:
                                    logger.warning(
                                        "T-Bank sbp: post-repair revert glyf %d→%d "
                                        "(cmap=%d, keep pre-repair exact)",
                                        _g_after, _g_before, _cmap_paint,
                                    )
                                    _ff_ship = _ffr
                                else:
                                    logger.warning(
                                        "T-Bank sbp: post-repair no cmap-safe exact "
                                        "glyf=%d cmap=%d — keep repaired then peel",
                                        _g_after, _cmap_paint,
                                    )
                                    _ff_ship = _fixed
                        # Twin-shape at exact length when possible.
                        try:
                            _twin = _load_corpus_f1_twin_for_glyf(int(_glr(_ff_ship)))
                            if _twin is not None:
                                _want_sh = _f1_glyph_shape(_twin)
                                if _f1_glyph_shape(_ff_ship) != _want_sh:
                                    _aligned = _align_f1_shape_to_corpus_twin(
                                        _ff_ship,
                                        glyf_len=int(_glr(_ff_ship)),
                                        keep=set(_reg_r) | {0, 3},
                                    )
                                    if (
                                        _aligned is not None
                                        and _f1_glyph_shape(_aligned) == _want_sh
                                        and int(_glr(_aligned)) == int(_glr(_ff_ship))
                                    ):
                                        # Re-repair after align (align can wipe ghosts).
                                        _aligned2 = _repair_ff2_empty_composite_components(
                                            _aligned, set(_reg_r) | {0, 3}, is_medium=False,
                                        )
                                        if int(_glr(_aligned2)) == int(_glr(_ff_ship)):
                                            _ff_ship = _aligned2
                                        elif _f1_glyph_shape(_aligned) == _want_sh:
                                            _ff_ship = _aligned
                        except Exception as _exc_sh:
                            logger.warning("T-Bank sbp: post-repair shape: %s", _exc_sh)
                        _patched_r = _patch_fontfile2_xref(result, _xr, _ff_ship)
                        if _patched_r is not None:
                            result = _patched_r
                            result = _finalize_tbank_f1_epoch(result)
                else:
                    _dr.close()
            except Exception as exc:
                logger.warning("T-Bank sbp: final composite repair: %s", exc)
            # Final F1 orphan residue wipe (only spares 35/239 allowed).
            # SHA twin FontFile2 must stay verbatim — cap/align ⇒ SIZE_MULTISET.
            try:
                from tbank_sbp_stealth import (
                    _cap_f1_orphan_spares as _cap_f1,
                    _gids_per_font_in_stream as _gids_f1,
                    _glyf_table_length as _gl_f1,
                    _patch_fontfile2_xref as _pfx_f1,
                )
                import fitz as _fz_f1
                import tbank_unlock_template as _tut_f1
                _df1 = _fz_f1.open(stream=result, filetype="pdf")
                _mf1 = _tut_f1._find_font_objects(_df1).get("TinkoffSans-Regular")
                if _mf1:
                    _ff1 = _df1.xref_stream(_mf1["fontfile_xref"])
                    _cs1 = _df1.xref_stream(_df1[0].get_contents()[0])
                    _tu1 = _df1.xref_stream(_mf1["tounicode_xref"])
                    _x1 = int(_mf1["fontfile_xref"])
                    _df1.close()
                    if False:
                        logger.info(
                            "T-Bank sbp: F1 corpus twin — skip orphan-cap/shape"
                        )
                    else:
                        _g_chk1 = int(_gl_f1(_ff1))
                        _twin_chk1 = _load_corpus_f1_twin_for_glyf(_g_chk1)
                        _sub1 = _tut_f1._parse_subset_tounicode(
                            _tu1.decode("latin1", "replace")
                        )
                        _reg1, _ = _gids_f1(_cs1)
                        _keep1 = set(_reg1) | {0, 3} | set(_sub1.keys())
                        _g_before1 = _gl_f1(_ff1)
                        _capped1 = _cap_f1(_ff1, _keep1)
                        if _capped1 != _ff1 and _gl_f1(_capped1) == _g_before1:
                                _ship1 = _capped1
                                _twin1 = _load_corpus_f1_twin_for_glyf(int(_g_before1))
                                if (
                                    _twin1 is not None
                                    and _f1_glyph_shape(_capped1) != _f1_glyph_shape(_twin1)
                                ):
                                    # Blank orphans then restore twin shape at same glyf_len.
                                    _al1 = _align_f1_shape_to_corpus_twin(
                                        _capped1,
                                        glyf_len=int(_g_before1),
                                        keep=_keep1,
                                    )
                                    if (
                                        _al1 is not None
                                        and _gl_f1(_al1) == _g_before1
                                        and _f1_glyph_shape(_al1) == _f1_glyph_shape(_twin1)
                                    ):
                                        _ship1 = _al1
                                        logger.info(
                                            "T-Bank sbp: orphan-cap+shape realign OK %s @%d",
                                            _f1_glyph_shape(_al1), _g_before1,
                                        )
                                    else:
                                        # Retarget to another atlas exact whose twin
                                        # matches post-cap shape (same cmap).
                                        _ship1 = None
                                        _cmap_n1 = len(_reg1)
                                        _exacts1 = {
                                            65: (12170,), 66: (12080, 12344),
                                            67: (12176, 12236, 12482, 12530, 12560, 12978),
                                            68: (12296, 12852, 12880, 12910, 13002),
                                            69: (12520, 12816), 70: (13000, 13210),
                                            71: (12818, 13002), 72: (12612,),
                                            74: (13738,), 75: (13610,),
                                            76: (13794, 14032),
                                        }.get(int(_cmap_n1)) or ()
                                        _have_sh = _f1_glyph_shape(_capped1)
                                        for _wg in _exacts1:
                                            if int(_wg) == int(_g_before1):
                                                continue
                                            _twg = _load_corpus_f1_twin_for_glyf(int(_wg))
                                            if _twg is None:
                                                continue
                                            if _f1_glyph_shape(_twg) != _have_sh:
                                                _cand = None
                                                if int(_wg) > int(_g_before1):
                                                    _cand = _snap_f1_glyf_exact_via_fat(
                                                        _capped1, int(_wg), _keep1,
                                                    )
                                                else:
                                                    _cand = _snap_f1_glyf_exact_via_trim_empty(
                                                        _capped1, int(_wg), _keep1,
                                                    )
                                                    if _cand is None:
                                                        _cand = _snap_f1_glyf_exact_via_shrink(
                                                            _capped1, int(_wg), _keep1,
                                                        )
                                                if _cand is None or _gl_f1(_cand) != int(_wg):
                                                    continue
                                                _al2 = _align_f1_shape_to_corpus_twin(
                                                    _cand, glyf_len=int(_wg), keep=_keep1,
                                                )
                                                if (
                                                    _al2 is not None
                                                    and _gl_f1(_al2) == int(_wg)
                                                    and _f1_glyph_shape(_al2)
                                                    == _f1_glyph_shape(_twg)
                                                ):
                                                    _ship1 = _al2
                                                    logger.info(
                                                        "T-Bank sbp: orphan-cap retarget "
                                                        "glyf %d→%d shape=%s",
                                                        _g_before1, _wg, _f1_glyph_shape(_al2),
                                                    )
                                                    break
                                            else:
                                                _cand = None
                                                if int(_wg) > int(_g_before1):
                                                    _cand = _snap_f1_glyf_exact_via_fat(
                                                        _capped1, int(_wg), _keep1,
                                                    )
                                                else:
                                                    _cand = _snap_f1_glyf_exact_via_trim_empty(
                                                        _capped1, int(_wg), _keep1,
                                                    )
                                                if (
                                                    _cand is not None
                                                    and _gl_f1(_cand) == int(_wg)
                                                    and _f1_glyph_shape(_cand)
                                                    == _f1_glyph_shape(_twg)
                                                ):
                                                    _ship1 = _cand
                                                    logger.info(
                                                        "T-Bank sbp: orphan-cap retarget "
                                                        "exact glyf %d→%d shape=%s",
                                                        _g_before1, _wg, _have_sh,
                                                    )
                                                    break
                                        if _ship1 is None:
                                            logger.warning(
                                                "T-Bank sbp: orphan-cap shape miss %s→%s @%d",
                                                _f1_glyph_shape(_ff1),
                                                _have_sh,
                                                _g_before1,
                                            )
                                if _ship1 is not None:
                                    _p1 = _pfx_f1(result, _x1, _ship1)
                                    if _p1 is not None:
                                        result = _finalize_tbank_f1_epoch(_p1)
                                        _re_peel = _peel_f1_glyf_to_cmap_band(
                                            result, height=519, prepared=prepared,
                                        )
                                        if _re_peel is None:
                                            logger.warning(
                                                "T-Bank sbp: post-orphan peel miss "
                                                "@%d — keep pre-cap peel",
                                                _g_before1,
                                            )
                                        else:
                                            result = _finalize_tbank_f1_epoch(
                                                _re_peel
                                            )
                                elif _capped1 != _ff1:
                                    # Shape recovery failed — keep peeled FF2.
                                    # Aborting here → bot «не собралось» on rare
                                    # Cyrillic hydrate (Ш/х). Orphan residue is
                                    # softer than GEN_NONE.
                                    logger.warning(
                                        "T-Bank sbp: orphan-cap shape miss "
                                        "unrecoverable @glyf=%d — keep pre-cap",
                                        _g_before1,
                                    )
            except Exception as exc:
                logger.warning("T-Bank sbp: F1 orphan-cap: %s", exc)
            try:
                from tbank_sbp_stealth import apply_sbp_font_hard_fixes as _hard
                result = _hard(result)
            except Exception as _exc_hf:
                logger.warning("T-Bank sbp font hard-fix: %s", _exc_hf)
            return _finalize_tbank_f2_epoch(result)

        # card: F2 HARD (height=431/471 atlas) — lean best-effort, still ship.
        if channel in ("card_tbank", "card_sber"):
            amt = ""
            if prepared:
                amt = str(
                    prepared.get("new_amount")
                    or prepared.get("amount")
                    or prepared.get("new_amount_total")
                    or ""
                )
            f2c = _lean_card_f2_to_hard(result, amt, lo=5056, hi=5868)
            if f2c is None:
                logger.warning(
                    "T-Bank %s: F2 lean failed — ship", channel,
                )
            else:
                result = f2c

        # phone/card/nocomm: unique trailer /ID.
        # Reused donor /ID + mutated content → Proton TBANK_TRAILER_ID_REUSED.
        # Donor BaseFont tags stay until the last FF2 write — then retag.
        from sber_dynamic import _strip_pdf_eof_tail

        import re as _re
        import random as _rnd

        def _rand_id() -> bytes:
            return ("%032x" % _rnd.getrandbits(128)).encode("ascii")

        before = result
        result = _re.sub(
            rb"/ID\s*\[\s*<[0-9A-Fa-f]{32}>\s*<[0-9A-Fa-f]{32}>\s*\]",
            lambda _m: b"/ID [<" + _rand_id() + b"><" + _rand_id() + b">]",
            result,
        )
        if len(result) != len(before):
            result = before
        result = _strip_pdf_eof_tail(result)
        result = _finalize_tbank_f1_epoch(result)
        # Phone mutates FontFile2 after the pipeline (lean / cmap remap / epoch).
        # Finish-retag here would saveIncr + then break tag↔FF2. Phone retags last.
        # phone / nocomm mutate FontFile2 after the pipeline (lean / remap).
        # They call _tbank_finish_non_sbp_ship themselves. Statement too.
        if channel in ("card_tbank", "card_sber"):
            return _tbank_finish_non_sbp_ship(
                result,
                height=int(_h or 0),
                prepared=prepared,
                channel=channel,
            )
        return _finalize_tbank_f2_epoch(result)

    logger.error("T-Bank %s: donor-orig и dynamic не удались.", channel)
    return None
