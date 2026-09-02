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
# Proton TBANK_VALUE_RIGHT_EDGE_OVERSHOOT is HARD above +0.01 pt
# (Jasper originals stay ≤0.005). 0.25 used to let +0.148 ship as FAKE.
_OVERSHOOT_HARD = 0.009
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
    """(baseline_y, overshoot) for value-column spans past R=250.

    Labels sit at x≈20 and values at x≈180–250. MuPDF often groups them as
    one line (x0<100). Proton scores only the value spans (x0≥100). Skip the
    label, keep the value x1 — otherwise nudge never sees footer amount/₽.
    """
    import fitz

    hits = []
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        for block in doc[0].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans") or []
                value_boxes = [
                    sp["bbox"]
                    for sp in spans
                    if sp.get("bbox") and float(sp["bbox"][0]) >= 100.0
                ]
                if not value_boxes:
                    continue
                x1 = max(float(b[2]) for b in value_boxes)
                over = x1 - _VALUE_RIGHT
                if over <= 0.005:
                    continue
                y0 = min(float(b[1]) for b in value_boxes)
                y1 = max(float(b[3]) for b in value_boxes)
                hits.append(((y0 + y1) / 2.0, over))
    finally:
        doc.close()
    return hits


def value_column_overshoot(pdf: bytes) -> float:
    """Max (line x1 − 250) for value-column lines (x0≥100)."""
    hits = _overflowing_value_lines(pdf)
    return max((h[1] for h in hits), default=0.0)


def f1_value_column_x1s(pdf: bytes) -> list:
    """Painted x1 of F1 Regular value-column spans (Proton SPREAD n=9).

    Skip labels (x0<100), Medium/₽, and amount digits that sit before ₽
    (x1≤244.2 — donor Сумма/комиссия). Remaining FIO/phone/bank/account/
    SBP-id/suffix/status must share one vertical.
    """
    import fitz

    out = []
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        for block in doc[0].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for sp in line.get("spans") or []:
                    bbox = sp.get("bbox") or [0, 0, 0, 0]
                    x0, _y0, x1, _y1 = (float(b) for b in bbox)
                    if x0 < 100.0:
                        continue
                    font = str(sp.get("font") or "")
                    if "Medium" in font or "ALSRubl" in font:
                        continue
                    if x1 <= 244.2:
                        continue
                    out.append((x0, x1, float(bbox[1]), str(sp.get("text") or "")))
    finally:
        doc.close()
    return out


def value_column_spread(pdf: bytes) -> float:
    """max(x1)−min(x1) of F1 value spans. Proton HARD above 2.0 pt."""
    xs = [t[1] for t in f1_value_column_x1s(pdf)]
    if len(xs) < 2:
        return 0.0
    return max(xs) - min(xs)


def pin_f1_value_column_spread_pdf(pdf: bytes, *, x0_slop: float = 0.08) -> bytes:
    """Shift F1 value Tm so painted x1 lands on 250 (RIGHT_EDGE_SPREAD).

    Equal-CID SBP-id inplace keeps donor Tm; a narrower live ID then
    undershoots by several pt. Measure bbox and move that Tm only.
    Amount digits before ₽ stay on 243.68 / 237.77.
    """
    import re as _re

    tm_re = _re.compile(rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm")
    right = _VALUE_RIGHT
    slop = float(x0_slop)
    for _round in range(4):
        spans = f1_value_column_x1s(pdf)
        drift = [
            (x0, x1) for x0, x1, _y, _t in spans if abs(x1 - right) > 0.05
        ]
        if not drift:
            return pdf
        import fitz

        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            xref = int(doc[0].get_contents()[0])
            stream = doc.xref_stream(xref)
        finally:
            doc.close()
        n = 0
        out = bytearray()
        pos = 0
        for m in tm_re.finditer(stream):
            out.extend(stream[pos:m.start()])
            tx = float(m.group(1))
            shifted = False
            if tx >= 50.0:
                best = None
                best_d = slop
                for x0, x1 in drift:
                    d = abs(tx - x0)
                    if d <= best_d:
                        best_d = d
                        best = (x0, x1)
                if best is not None:
                    x0, x1 = best
                    new_x = tx + (right - x1)
                    out.extend(
                        b"1 0 0 1 "
                        + jasper_fmt_token(new_x).encode("ascii")
                        + b" "
                        + m.group(2)
                        + b" Tm"
                    )
                    n += 1
                    shifted = True
            if not shifted:
                out.extend(m.group(0))
            pos = m.end()
        out.extend(stream[pos:])
        if not n:
            logger.warning(
                "Jasper value-column spread %.3f but no Tm matched x0",
                value_column_spread(pdf),
            )
            return pdf
        committed = _commit_contents_inplace(pdf, xref, bytes(out))
        if committed is None:
            return pdf
        pdf = committed
        logger.info("Jasper value-column spread pin %d Tm", n)
    return pdf


def _value_column_line_edges(pdf: bytes):
    """Proton line-level value-column x1 (x0≥100, x1≥180) + page height."""
    import fitz

    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        height = float(doc[0].rect.height)
        d = doc[0].get_text("dict")
    finally:
        doc.close()
    lines = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            if not spans:
                continue
            txt = "".join(sp.get("text", "") for sp in spans).strip()
            if not txt or len(txt) < 2:
                continue
            boxes = [sp["bbox"] for sp in spans if sp.get("bbox")]
            if not boxes:
                continue
            x0 = min(float(b[0]) for b in boxes)
            x1 = max(float(b[2]) for b in boxes)
            y0 = min(float(b[1]) for b in boxes)
            y1 = max(float(b[3]) for b in boxes)
            if x0 < 100 or x1 < 180:
                continue
            lines.append((x0, x1, y0, y1))
    return lines, height


def value_column_lattice_off(pdf: bytes) -> list:
    """Quantized residuals not in {-1.0, 0.0} (Proton OFF_LATTICE)."""
    lines, height = _value_column_line_edges(pdf)
    if height > 499.0 or len(lines) < 4:
        return []
    off = []
    for _x0, x1, _y0, _y1 in lines:
        r = x1 - _VALUE_RIGHT
        q = round(round(r / 0.05) * 0.05, 2)
        if q not in (-1.0, 0.0):
            off.append(q)
    return sorted(set(off))


def snap_value_column_lattice_pdf(pdf: bytes) -> bytes:
    """card/phone: line x1 residual must quantize to {-1.0, 0.0} (0.05 pt).

    Proton measures the whole line bbox (amount+₽), not F1 spans alone.
    Shift the value Tm whose x matches the line's x0 (donor values start
    at 181–237). Do not steal the rightmost Tm on a ±14 pt y-band — that
    pulled neighbouring rows off R=250.
    """
    import re as _re

    tm_re = _re.compile(rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm")
    right = _VALUE_RIGHT
    allowed = (-1.0, 0.0)

    for _round in range(6):
        lines, height = _value_column_line_edges(pdf)
        if height > 499.0:
            return pdf
        drift = []
        for x0, x1, y0, y1 in lines:
            r = x1 - right
            q = round(round(r / 0.05) * 0.05, 2)
            if q in allowed:
                continue
            tgt_r = -1.0 if r < -0.5 else 0.0
            drift.append((x0, x1, (y0 + y1) / 2.0, right + tgt_r))
        if not drift:
            return pdf
        import fitz

        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            xref = int(doc[0].get_contents()[0])
            stream = doc.xref_stream(xref)
        finally:
            doc.close()
        tms = [
            (m.start(), m.end(), float(m.group(1)), float(m.group(2)), m.group(2))
            for m in tm_re.finditer(stream)
        ]
        shifts = {}
        used = set()
        for lx0, x1, ymid, tgt in drift:
            delta = tgt - x1
            cands = [
                (i, tx, ty, abs(tx - lx0), abs(ty - ymid))
                for i, (_s, _e, tx, ty, _yb) in enumerate(tms)
                if tx >= 50.0 and i not in used and abs(ty - ymid) <= 8.0
            ]
            if not cands:
                continue
            # Shift the rightmost value Tm on the row — line x1 follows max x,
            # not the left anchor Tm (Proton OFF_LATTICE -0.05 on phone).
            i, tx, _ty, _dx, _dy = max(cands, key=lambda t: (t[1], -t[3]))
            shifts[i] = tx + delta
            used.add(i)
        if not shifts:
            logger.warning("Jasper value lattice residual off but no Tm on row")
            return pdf
        out = bytearray()
        pos = 0
        for i, (s, e, tx, _ty, yb) in enumerate(tms):
            out.extend(stream[pos:s])
            if i in shifts:
                out.extend(
                    b"1 0 0 1 "
                    + jasper_fmt_token(shifts[i]).encode("ascii")
                    + b" "
                    + yb
                    + b" Tm"
                )
            else:
                out.extend(stream[s:e])
            pos = e
        out.extend(stream[pos:])
        committed = _commit_contents_inplace(pdf, xref, bytes(out))
        if committed is None:
            return pdf
        pdf = committed
        logger.info("Jasper value-column lattice snap %d Tm", len(shifts))
    return pdf


def _commit_contents_inplace(pdf: bytes, xref: int, new_stream: bytes) -> Optional[bytes]:
    """Swap decoded Contents without fitz.tobytes.

    Prefer same compressed /Length. If a Tm nudge changes flate size, update
    /Length + xref rather than silently keeping the overflowing layout
    (Proton TBANK_VALUE_RIGHT_EDGE_OVERSHOOT HARD > +0.01).
    """
    from tbank_orig_mode import _find_stream_pos_for_xref
    from tbank_stealth_v3 import (
        _best_compress,
        _pad_to_compressed_size,
        _patch_length_and_rebuild,
    )

    pos = _find_stream_pos_for_xref(pdf, int(xref))
    if not pos:
        return None
    cs, ce = pos
    orig_len = ce - cs
    packed = _pad_to_compressed_size(bytes(new_stream), orig_len)
    if packed is not None and len(packed) == orig_len:
        return pdf[:cs] + packed + pdf[ce:]
    packed = _best_compress(bytes(new_stream))
    if not packed:
        return None
    rebuilt = _patch_length_and_rebuild(bytearray(pdf), cs, ce, packed)
    if rebuilt is None:
        logger.warning("Jasper Contents /Length rebuild failed after Tm nudge")
        return None
    return rebuilt


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
            committed = _commit_contents_inplace(pdf, xref, bytes(out))
            if committed is None:
                return pdf
            logger.info("Jasper Tm sanitize %d tokens", n)
            return committed
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
            committed = _commit_contents_inplace(pdf, xref, bytes(out))
            if committed is None:
                return pdf
            logger.info("SBP amount Y repin %d slot(s)", n)
            return committed
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

    for _round in range(8):
        hits = _overflowing_value_lines(pdf)
        if not hits:
            return pdf
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            xref = doc[0].get_contents()[0]
            stream = doc.xref_stream(xref)
            page_h = float(doc[0].mediabox.y1)
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
                        if (
                            abs(ly - y) <= 14.0
                            or abs((page_h - ly) - y) <= 14.0
                        ):
                            delta = max(delta, over + 0.006)
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
                delta = max_over + 0.006
                stream = (
                    stream[:best.start()]
                    + b"1 0 0 1 " + _x_tok(best_x - delta) + b" " + best.group(2) + b" Tm"
                    + stream[best.end():]
                )
                n = 1
                out = bytearray(stream)
            committed = _commit_contents_inplace(pdf, xref, bytes(out))
            if committed is None:
                return pdf
            pdf = committed
            logger.info(
                "Jasper value-column nudge %d Tm (max +%.3f)",
                n, max(h[1] for h in hits),
            )
        finally:
            doc.close()
    return pdf


def polish_layout_pdf(pdf: bytes, *, channel: str = "sbp") -> bytes:
    """Last Contents pass: Jasper Tm tokens, R=250 pin, SBP amount Y baselines.

    Card/phone/nocomm: lattice snap only. The +0.006 overshoot nudge fights
    Proton residual lattice {-1, 0} and walks x1 to -1.7/-0.1.
    """
    out = sanitize_contents_tm(pdf)
    if channel == "sbp":
        out = nudge_value_column_pdf(out)
        out = repin_sbp_amount_y_pdf(out)
        out = nudge_value_column_pdf(out)
        out = pin_f1_value_column_spread_pdf(out)
    elif channel == "phone":
        # Generic lattice snap breaks phone: commission Tm flies off-page,
        # «Телефон получателя» drifts left, stamp « ET» corrupts.
        pass
    elif channel in (
        "card_sber", "card_tbank", "nocomm", "statement",
    ):
        out = snap_value_column_lattice_pdf(out)
        if value_column_lattice_off(out):
            out = snap_value_column_lattice_pdf(out)
        if value_column_overshoot(out) > _OVERSHOOT_HARD:
            out = nudge_value_column_pdf(out)
            out = snap_value_column_lattice_pdf(out)
    over = value_column_overshoot(out)
    if over > 0.005:
        logger.warning(
            "Jasper layout polish still right-overshoot +%.3f after nudge",
            over,
        )
    spread = value_column_spread(out)
    if spread > 2.0:
        logger.warning(
            "Jasper layout polish still value-column spread %.3f",
            spread,
        )
    return out


def emit_invariants(pdf: bytes, *, channel: str = "sbp") -> str:
    """Empty string = Jasper subset closed. Else rebuild reason (Alfa emit_invariants)."""
    import fitz
    import tbank_unlock_template as tut
    from tbank_dynamic import (
        _load_corpus_f1_twin_for_glyf,
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
    if g and _load_corpus_f1_twin_for_glyf(g) is not None:
        if not _tbank_ff2_is_corpus_twin(ff1, height=h):
            return f"glyph mismatch mutated-twin-glyf:{g}@{cmap_n}"
    # Proton TBANK_F1_GLYF_CMAP_OUTLIER — hydrate/unpeeled glyf outside atlas.
    if g and not _tbank_ff2_is_corpus_twin(ff1, height=h):
        _band = {
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
                59: (11088, 11102), 60: (11142, 11142), 61: (11614, 11620),
                62: (11302, 11342), 63: (11732, 11732),
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
        }.get(int(h), {}).get(int(cmap_n))
        if _band and not (_band[0] - 32 <= g <= _band[1] + 32):
            return f"glyph mismatch glyf-cmap-outlier:{g}@{cmap_n}"
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
