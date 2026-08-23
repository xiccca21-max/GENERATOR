"""Jasper/OpenPDF subset emitter for T-Bank — same role as alfa_emit for Oracle.

Alfa: Subset(source Tahoma, first-paint), CID 1..N, natural FontFile2 length.
T-Bank: Subset(source TinkoffSans, first-paint), Jasper GIDs (А=235…), natural
glyf length. Receipt faces differ; the generator function is the same as the
bank's: never copy another receipt's glyf_len while swapping glyph inventory.

Proton TBANK_F1_GLYF_SHAPE_ENVELOPE / SIZE_MULTISET fire when SEQ pads a
hydrated FontFile2 onto a corpus twin length (e.g. 13000) with different
letters. Native Jasper subset does not do that.
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_VALUE_RIGHT = 250.0
_OVERSHOOT_HARD = 0.01
_RIGHT_EDGE_INSET_PT = 0.25
_AMOUNT_SM_RIGHT_MAX = 243.68
_AMOUNT_BG_RIGHT_MAX = 237.77
_SBP_AMOUNT_Y_SM = 336.78
_SBP_AMOUNT_Y_BG = 412.39


def subset_f1_native(
    ff2: bytes,
    keep_gids: Iterable[int],
) -> bytes:
    """Subset(TinkoffSans shell, first-paint) — Jasper GID-stable, no length pad.

    Blank unused slots, loca-collapse non-genuine orphans, pin CSA/epoch.
    Length is whatever this painted set produces.
    """
    from tbank_sbp_stealth import (
        _blank_ff2_unused_glyfs,
        _cap_f1_orphan_spares,
        _ff2_composite_closure,
        _force_tbank_f1_head_epoch,
    )

    keep = _ff2_composite_closure(ff2, set(keep_gids) | {0, 3})
    if 248 in keep:
        keep.add(57)
    out = _blank_ff2_unused_glyfs(ff2, keep)
    out = _cap_f1_orphan_spares(out, keep)
    return _force_tbank_f1_head_epoch(out)


def _escape_mutated_atlas_exact(
    ff2: bytes,
    keep: Set[int],
    exacts: Iterable[int],
) -> Optional[bytes]:
    """Leave a closed-atlas glyf_len without padding onto another exact."""
    from tbank_sbp_stealth import _glyf_table_length
    from tbank_dynamic import _snap_f1_glyf_exact_via_trim_empty

    banned = {int(x) for x in exacts}
    native = subset_f1_native(ff2, keep)
    g = int(_glyf_table_length(native))
    if g not in banned:
        return native
    # Reverse SEQ fat-pad: trim empty loca until length is not a twin exact.
    for want in range(g - 2, max(8000, g - 512), -2):
        if want in banned:
            continue
        trimmed = _snap_f1_glyf_exact_via_trim_empty(
            native, want, keep, allow_fat=False,
        )
        if trimmed is None:
            continue
        got = int(_glyf_table_length(trimmed))
        if got in banned:
            continue
        logger.info("F1 Jasper-native escape atlas-exact %d→%d", g, got)
        return trimmed
    logger.warning("F1 Jasper-native cannot leave atlas-exact glyf=%d", g)
    return None


def _land_native_glyf_in_cmap_band(
    ff2: bytes,
    keep: Set[int],
    cmap_n: int,
    xs: Tuple[int, ...],
) -> Optional[bytes]:
    """Grow/trim a hydrated native F1 into the cmap envelope, never onto an exact.

    Ю / rare letters hydrate past the band, then orphan-blank undershoots it.
    Envelope ±32 is HARD; exact twin lengths are SHAPE/SIZE. Land in between.
    """
    from tbank_dynamic import (
        _snap_f1_glyf_exact_via_fat,
        _snap_f1_glyf_exact_via_trim_empty,
    )
    from tbank_sbp_stealth import _glyf_table_length

    if not xs:
        return ff2
    banned = {int(x) for x in xs}
    lo, hi = min(banned) - 32, max(banned) + 32
    g = int(_glyf_table_length(ff2))
    if lo <= g <= hi and g not in banned:
        return ff2
    if g in banned:
        esc = _escape_mutated_atlas_exact(ff2, keep, xs)
        if esc is not None:
            ge = int(_glyf_table_length(esc))
            if lo <= ge <= hi and ge not in banned:
                return esc
    mid = (min(banned) + max(banned)) // 2
    targets: List[int] = []
    for t in (mid, mid - 4, mid + 4, lo + 4, hi - 4, 12998, 13104, 13180):
        t = int(t)
        if t in banned or t in targets:
            continue
        if lo <= t <= hi:
            targets.append(t)
    for t in targets:
        if g < t:
            grown = _snap_f1_glyf_exact_via_fat(ff2, t, keep, prefer_keep=True)
            if grown is None:
                continue
            ge = int(_glyf_table_length(grown))
            if lo <= ge <= hi and ge not in banned:
                logger.info(
                    "F1 land-in-band grow %d→%d cmap=%d want=%d",
                    g, ge, cmap_n, t,
                )
                return grown
        elif g > t:
            trimmed = _snap_f1_glyf_exact_via_trim_empty(
                ff2, t, keep, allow_fat=False,
            )
            if trimmed is None:
                continue
            ge = int(_glyf_table_length(trimmed))
            if lo <= ge <= hi and ge not in banned:
                logger.info(
                    "F1 land-in-band trim %d→%d cmap=%d want=%d",
                    g, ge, cmap_n, t,
                )
                return trimmed
    return None


def ship_jasper_native_f1(
    pdf: bytes,
    ff_xref: int,
    ff2: bytes,
    *,
    cs: bytes,
    cmap_n: int,
    height: int,
    exacts: Dict[int, Tuple[int, ...]],
    glyf_len: int,
    uni_plan: Optional[Dict[int, int]] = None,
) -> Optional[bytes]:
    """Covering SHA twin, or native subset only if glyf sits in the cmap band.

    Never SEQ-pad a mutated inventory onto a twin length (SHAPE_ENVELOPE).
    Never ship native glyf outside Proton cmap band (CMAP_OUTLIER).
    """
    from tbank_dynamic import (
        _load_corpus_f1_twin_for_glyf,
        _ship_f1_at_exact_twin_shape,
        _tbank_ff2_is_corpus_twin,
    )
    from tbank_sbp_stealth import (
        _ff2_composite_closure,
        _ff2_keep_renders,
        _gids_per_font_in_stream,
        _glyf_table_length,
        _patch_fontfile2_xref,
    )

    keep: Set[int] = {0, 3}
    if cs:
        try:
            reg, _med = _gids_per_font_in_stream(cs)
            keep |= {int(x) for x in reg}
        except Exception:
            pass
    keep = _ff2_composite_closure(ff2, keep)
    if 248 in keep:
        keep.add(57)

    xs = tuple(int(x) for x in (exacts.get(int(cmap_n)) or ()))

    def _in_band(g: int, cn: int) -> bool:
        ys = tuple(int(x) for x in (exacts.get(int(cn)) or ()))
        if not ys:
            return True
        return (min(ys) - 32) <= int(g) <= (max(ys) + 32)

    def _cover(want: int) -> Optional[bytes]:
        twin = _load_corpus_f1_twin_for_glyf(int(want))
        if twin is None:
            return None
        if not _tbank_ff2_is_corpus_twin(twin, height=height):
            return None
        if not _ff2_keep_renders(twin, keep):
            return None
        return twin

    lo_g = (min(xs) - 32) if xs else None
    hi_g = (max(xs) + 32) if xs else None
    if uni_plan and lo_g is not None:
        try:
            from tbank_sbp_stealth import _pick_covering_corpus_twin_ff2
            alt = _pick_covering_corpus_twin_ff2(
                set(uni_plan.keys()), uni_plan,
                glyf_min=lo_g, glyf_max=hi_g,
            )
        except Exception:
            alt = None
        if alt is not None and _tbank_ff2_is_corpus_twin(alt, height=height):
            ag = int(_glyf_table_length(alt))
            if _in_band(ag, cmap_n) and _ff2_keep_renders(alt, keep):
                patched = _patch_fontfile2_xref(pdf, ff_xref, alt)
                if patched is not None:
                    logger.info(
                        "F1 covering SHA twin glyf=%d cmap=%d h=%d (picker)",
                        ag, cmap_n, height,
                    )
                    return patched

    inband_wants = []
    for _cn, ys in exacts.items():
        for want in ys:
            if _in_band(int(want), cmap_n):
                inband_wants.append(int(want))
    mid = (min(xs) + max(xs)) / 2.0 if xs else 0.0
    xs_set = set(xs)
    inband_wants = sorted(
        set(inband_wants),
        key=lambda w: (0 if w in xs_set else 1, abs(w - mid), w),
    )
    for want in inband_wants:
        twin = _cover(want)
        if twin is None:
            continue
        patched = _patch_fontfile2_xref(pdf, ff_xref, twin)
        if patched is not None:
            logger.info(
                "F1 covering SHA twin glyf=%d cmap=%d h=%d",
                want, cmap_n, height,
            )
            return patched

    native = subset_f1_native(ff2, keep)
    g = int(_glyf_table_length(native))

    if _tbank_ff2_is_corpus_twin(native, height=height) and _in_band(g, cmap_n):
        patched = _patch_fontfile2_xref(pdf, ff_xref, native)
        return patched if patched is not None else pdf

    if _in_band(g, cmap_n) and g not in xs:
        patched = _patch_fontfile2_xref(pdf, ff_xref, native)
        if patched is not None:
            logger.info(
                "F1 Jasper-native in-band glyf %d→%d cmap=%d h=%d",
                glyf_len, g, cmap_n, height,
            )
            return patched
        return pdf

    if g in xs:
        shipped = _ship_f1_at_exact_twin_shape(
            pdf, ff_xref, native,
            glyf_len=g, cmap_n=int(cmap_n),
            keep=keep, height=height, why="jasper-native",
        )
        if shipped is not None:
            return shipped
        escaped = _escape_mutated_atlas_exact(native, keep, xs)
        if escaped is not None:
            ge = int(_glyf_table_length(escaped))
            if _in_band(ge, cmap_n) and ge not in xs:
                patched = _patch_fontfile2_xref(pdf, ff_xref, escaped)
                if patched is not None:
                    return patched
        logger.warning(
            "F1 Jasper-native mutated exact glyf=%d cmap=%d — reject",
            g, cmap_n,
        )
        return None

    fat_wants = sorted(
        {int(w) for ys in exacts.values() for w in ys},
        reverse=True,
    )
    for want in fat_wants:
        parent = _cover(want)
        if parent is None:
            continue
        nat2 = subset_f1_native(parent, keep)
        g2 = int(_glyf_table_length(nat2))
        if _in_band(g2, cmap_n) and g2 not in xs:
            patched = _patch_fontfile2_xref(pdf, ff_xref, nat2)
            if patched is not None:
                logger.info(
                    "F1 native-from-covering-parent glyf=%d cmap=%d (parent %d)",
                    g2, cmap_n, want,
                )
                return patched
        if g2 in xs:
            escaped = _escape_mutated_atlas_exact(nat2, keep, xs)
            if escaped is not None:
                ge = int(_glyf_table_length(escaped))
                if _in_band(ge, cmap_n) and ge not in xs:
                    patched = _patch_fontfile2_xref(pdf, ff_xref, escaped)
                    if patched is not None:
                        return patched

    landed = _land_native_glyf_in_cmap_band(native, keep, cmap_n, xs)
    if landed is not None:
        ge = int(_glyf_table_length(landed))
        if _in_band(ge, cmap_n) and ge not in xs:
            patched = _patch_fontfile2_xref(pdf, ff_xref, landed)
            if patched is not None:
                logger.info(
                    "F1 Jasper-native land-in-band glyf %d→%d cmap=%d h=%d",
                    g, ge, cmap_n, height,
                )
                return patched
    logger.warning(
        "F1 Jasper-native out of atlas glyf=%d cmap=%d — reject (no CMAP_OUTLIER ship)",
        g, cmap_n,
    )
    return None


# Labels live near x=20. Value Tms (including long FIO / SBP id) start ≥50.
_SBP_ID_Y_BAND = (0.0, -1.0)  # disabled — overflowing SBP-id must be shifted


def _overflowing_value_lines(pdf: bytes) -> list:
    """(baseline_y, overshoot) for value-column lines past R=250."""
    import fitz

    hits = []
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        for block in doc[0].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans") or []
                boxes = [sp["bbox"] for sp in spans if sp.get("bbox")]
                if not boxes:
                    continue
                x0 = min(float(b[0]) for b in boxes)
                x1 = max(float(b[2]) for b in boxes)
                if x0 < 100 or x1 < 180:
                    continue
                over = x1 - _VALUE_RIGHT
                if over <= 0.005:
                    continue
                y0 = min(float(b[1]) for b in boxes)
                y1 = max(float(b[3]) for b in boxes)
                hits.append(((y0 + y1) / 2.0, over))
    finally:
        doc.close()
    return hits


def value_column_overshoot(pdf: bytes) -> float:
    """Max (line x1 − 250) for value-column lines (x0≥100)."""
    hits = _overflowing_value_lines(pdf)
    return max((h[1] for h in hits), default=0.0)


def jasper_fmt_token(v: float) -> str:
    """OpenPDF: 187.1 not 187.10; 216.54 not 216.545; 172 not 172.00."""
    s = f"{float(v):.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _tm_token_ok(raw: bytes) -> bool:
    s = raw.decode("ascii")
    if s.count(".") > 1:
        return False
    if "." not in s:
        return True
    frac = s.split(".", 1)[1]
    if not frac or len(frac) > 2:
        return False
    if frac.endswith("0"):
        return False
    return True


def sanitize_contents_tm(pdf: bytes) -> bytes:
    """Rewrite every Tm x/y to Jasper OpenPDF form. Last Contents pass."""
    import fitz
    import re as _re

    tm_re = _re.compile(rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm")
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception:
        return pdf
    try:
        xref = doc[0].get_contents()[0]
        stream = doc.xref_stream(xref)
        out = bytearray()
        pos = 0
        n = 0
        for m in tm_re.finditer(stream):
            out.extend(stream[pos:m.start()])
            if _tm_token_ok(m.group(1)) and _tm_token_ok(m.group(2)):
                out.extend(m.group(0))
            else:
                xs = jasper_fmt_token(float(m.group(1))).encode("ascii")
                ys = jasper_fmt_token(float(m.group(2))).encode("ascii")
                out.extend(b"1 0 0 1 " + xs + b" " + ys + b" Tm")
                n += 1
            pos = m.end()
        out.extend(stream[pos:])
        if n:
            doc.update_stream(xref, bytes(out))
            pdf = doc.tobytes(deflate=False, garbage=0, clean=False)
            logger.info("Jasper Tm sanitize %d tokens", n)
        return pdf
    finally:
        doc.close()


def repin_sbp_amount_y_pdf(pdf: bytes) -> bytes:
    """Pin Сумма/Итого value Tm Y to donor baselines (336.78 / 412.39).

    Labels and values share one row Y in T_sbp_original; diversify/realign
    must not leave value digits on a drifted baseline.
    """
    import fitz
    import re as _re

    block_re = _re.compile(
        rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm\n"
        rb"/(F[12]) ([0-9.]+) Tf\n"
        rb"0\.2 0\.2 0\.2 rg\n"
        rb"\((?:\\.|[^\\)])*\)Tj"
    )
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception:
        return pdf
    try:
        xref = doc[0].get_contents()[0]
        stream = doc.xref_stream(xref)
        out = bytearray()
        pos = 0
        n = 0
        for m in block_re.finditer(stream):
            out.extend(stream[pos:m.start()])
            x = float(m.group(1))
            y = float(m.group(2))
            ftag = m.group(3)
            sz = float(m.group(4))
            y_tgt = None
            if ftag == b"F2" and abs(sz - 16.0) < 0.01 and x >= 50.0:
                y_tgt = _SBP_AMOUNT_Y_BG
            elif ftag == b"F1" and abs(sz - 9.0) < 0.01 and x >= 80.0:
                if 330.0 <= y <= 345.0:
                    y_tgt = _SBP_AMOUNT_Y_SM
            if y_tgt is not None and abs(y - y_tgt) > 0.015:
                ys = jasper_fmt_token(y_tgt).encode("ascii")
                tail = m.group(0)[m.group(0).find(b"0.2 0.2 0.2 rg\n"):]
                out.extend(
                    b"1 0 0 1 "
                    + m.group(1)
                    + b" "
                    + ys
                    + b" Tm\n/"
                    + ftag
                    + b" "
                    + m.group(4)
                    + b" Tf\n"
                    + tail
                )
                n += 1
            else:
                out.extend(m.group(0))
            pos = m.end()
        out.extend(stream[pos:])
        if n:
            doc.update_stream(xref, bytes(out))
            pdf = doc.tobytes(deflate=False, garbage=0, clean=False)
            logger.info("SBP amount Y repin %d slot(s)", n)
        return pdf
    finally:
        doc.close()


def nudge_value_column_pdf(pdf: bytes) -> bytes:
    """Shift only overflowing value Tm so line x1 ≤ R=250.

    Never touch SBP-id/account Y and never truncate Tm tokens — a global
    shift / chopped X made SBP_CIPHER_MISSING and bot GEN_NONE.
    """
    import fitz
    import re as _re

    tm_re = _re.compile(rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm")

    def _x_tok(new_x: float) -> bytes:
        return jasper_fmt_token(new_x).encode("ascii")

    for _round in range(3):
        hits = _overflowing_value_lines(pdf)
        if not hits:
            return pdf
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            xref = doc[0].get_contents()[0]
            stream = doc.xref_stream(xref)
            out = bytearray()
            pos = 0
            n = 0
            for m in tm_re.finditer(stream):
                out.extend(stream[pos:m.start()])
                x = float(m.group(1))
                y = float(m.group(2))
                delta = 0.0
                if x >= 50.0:
                    for ly, over in hits:
                        if abs(ly - y) <= 14.0 or abs((519.0 - ly) - y) <= 14.0:
                            delta = max(delta, over + 0.02)
                if delta > 0:
                    out.extend(
                        b"1 0 0 1 " + _x_tok(x - delta) + b" " + m.group(2) + b" Tm"
                    )
                    n += 1
                else:
                    out.extend(m.group(0))
                pos = m.end()
            out.extend(stream[pos:])
            if not n:
                max_over = max(h[1] for h in hits)
                best = None
                best_x = -1.0
                for m2 in tm_re.finditer(stream):
                    x2 = float(m2.group(1))
                    y2 = float(m2.group(2))
                    if x2 >= 50.0 and x2 > best_x:
                        best_x = x2
                        best = m2
                if best is None:
                    logger.warning(
                        "Jasper value-column overshoot +%.3f but no Tm to nudge",
                        max_over,
                    )
                    return pdf
                delta = max_over + 0.02
                stream = (
                    stream[:best.start()]
                    + b"1 0 0 1 " + _x_tok(best_x - delta) + b" " + best.group(2) + b" Tm"
                    + stream[best.end():]
                )
                n = 1
                out = bytearray(stream)
            doc.update_stream(xref, bytes(out))
            pdf = doc.tobytes(deflate=False, garbage=0, clean=False)
            logger.info(
                "Jasper value-column nudge %d Tm (max +%.3f)",
                n, max(h[1] for h in hits),
            )
        finally:
            doc.close()
    return pdf


def emit_invariants(pdf: bytes, *, channel: str = "sbp") -> str:
    """Empty string = Jasper subset closed. Else rebuild reason (Alfa emit_invariants)."""
    import fitz
    import tbank_unlock_template as tut
    from tbank_dynamic import (
        _F1_GLYF_CMAP_EXACTS,
        _tbank_ff2_is_corpus_twin,
    )
    from tbank_sbp_stealth import _glyf_table_length

    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception as exc:
        return f"xref/Length mismatch {exc}"
    try:
        h = int(round(float(doc[0].mediabox.y1)))
        fr = tut._find_font_objects(doc).get("TinkoffSans-Regular")
        if not fr:
            return "glyph mismatch missing-f1"
        ff1 = doc.xref_stream(fr["fontfile_xref"])
        tu = doc.xref_stream(fr["tounicode_xref"])
        sub = tut._parse_subset_tounicode(tu.decode("latin1", "replace"))
        cmap_n = len(sub)
    except Exception as exc:
        doc.close()
        return f"glyph mismatch parse:{exc}"
    doc.close()

    over = value_column_overshoot(pdf)
    if over > _OVERSHOOT_HARD:
        return f"layout mismatch right-overshoot:+{over:.3f}"
    if channel == "sbp":
        from tbank_sbp_stealth import _extract_sbp_opid_flat as _cip_inv
        if _cip_inv(pdf) is None:
            return "layout mismatch sbp-cipher-missing"
    import re as _re_tm
    try:
        import fitz as _fz_tm
        _dtm = _fz_tm.open(stream=pdf, filetype="pdf")
        _cstm = _dtm.xref_stream(_dtm[0].get_contents()[0])
        _dtm.close()
        for _m in _re_tm.finditer(rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm", _cstm):
            if not _tm_token_ok(_m.group(1)) or not _tm_token_ok(_m.group(2)):
                return (
                    "layout mismatch tm-token:"
                    f"{_m.group(1).decode()}/{_m.group(2).decode()}"
                )
    except Exception:
        pass

    g = int(_glyf_table_length(ff1)) if ff1 else 0
    xs = (_F1_GLYF_CMAP_EXACTS.get(int(h)) or {}).get(int(cmap_n)) or ()
    if xs and not _tbank_ff2_is_corpus_twin(ff1, height=h):
        lo, hi = min(int(x) for x in xs) - 32, max(int(x) for x in xs) + 32
        if not (lo <= g <= hi):
            return f"glyph mismatch cmap-outlier:{g}@{cmap_n}"
        if g in xs:
            return f"glyph mismatch mutated-twin-glyf:{g}@{cmap_n}"
    try:
        from tbank_dynamic import _f1_loca_tables
        offs, glyf_ba, _itl, _ng = _f1_loca_tables(ff1)
        loca_end = int(offs[-1]) if offs else 0
        pad = len(glyf_ba) - loca_end
        if pad > 0:
            return f"glyph mismatch trailing-junk:{pad}"
    except Exception:
        pass
    try:
        from tbank_sbp_stealth import _ff2_zero_contour_stub_gids
        stubs = [g for g in _ff2_zero_contour_stub_gids(ff1) if int(g) not in (0, 3)]
        if stubs:
            return f"glyph mismatch zero-contour-stub:{stubs[:8]}"
    except Exception:
        pass
    try:
        import fitz as _fz_fl
        import re as _re_fl
        _dfl = _fz_fl.open(stream=pdf, filetype="pdf")
        _csfl = _dfl.xref_stream(_dfl[0].get_contents()[0])
        _dfl.close()
        _tmp = _re_fl.sub(rb"\((?:\\.|[^\\)])*\)", b"()", _csfl)
        for _m in _re_fl.findall(rb"(?<![\d.])(-?\d+\.\d+)(?![\d])", _tmp):
            _frac = _m.split(b".", 1)[1]
            if _frac and len(_frac) >= 2 and _frac.endswith(b"0"):
                return f"layout mismatch float-trailing-zero:{_m.decode()}"
    except Exception:
        pass
    return ""
