# -*- coding: utf-8 -*-
"""Bake Alfa unlocked full-charset shells (phone / card / sbp)."""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

logger = logging.getLogger(__name__)

NEED = (
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "0123456789"
)


def _pdf_charset(path: str) -> set:
    import fitz
    import tbank_unlock_template as tut

    try:
        d = fitz.open(path)
        fonts = tut._find_font_objects(d)
        best: set = set()
        for fm in fonts.values():
            tu = tut._parse_subset_tounicode(
                d.xref_stream(fm["tounicode_xref"]).decode("latin1", "replace")
            )
            chars = {chr(u) for u in tu.values() if u >= 0x20}
            if len(chars) > len(best):
                best = chars
        d.close()
        return best
    except Exception:
        return set()


def _rank_seed(kind: str, donors: list) -> str:
    """Prefer layout-ok card + donors that already have hard glyphs (й)."""
    scored = []
    for p in donors[:40]:
        if not p or not os.path.isfile(p):
            continue
        if kind == "card":
            try:
                from alfa_card_stealth import _donor_layout_ok
                from alfa_orig_mode import AlfaOrigContext

                ctx = AlfaOrigContext()
                if not (ctx.load(p) and _donor_layout_ok(ctx)):
                    continue
            except Exception:
                continue
        chars = _pdf_charset(p)
        rare = sum(1 for ch in "йЙёЁъыэщШЩ" if ch in chars)
        scored.append((1 if "й" in chars else 0, rare, len(chars), p))
    if not scored:
        return donors[0]
    scored.sort(reverse=True)
    return scored[0][3]


def bake_alfa_kind(kind: str) -> Optional[Path]:
    """kind: phone|card|sbp → templates/Alfa_{kind}_unlocked.pdf"""
    import alfa_font_extend as afe
    import alfa_glyph_library as agl
    from alfa_corpus import canonical_paths, rank_donors

    kind = kind.lower().replace("alfa_", "")
    out = _ROOT / "templates" / f"Alfa_{kind}_unlocked.pdf"
    donors = list(rank_donors(kind) or canonical_paths(kind) or [])
    if not donors:
        logger.error("alfa bake: no donors for %s", kind)
        return None

    agl.ensure_library()
    best = _rank_seed(kind, donors)
    best_n = len(_pdf_charset(best))

    work = _ROOT / f"_tmp_alfa_{kind}_unlock.pdf"
    shutil.copy2(best, work)
    pool = [p for p in donors if p and os.path.isfile(p)][:25]
    # Prefer pool members that already carry rare glyphs for inject source
    pool.sort(key=lambda p: (1 if "й" in _pdf_charset(p) else 0, len(_pdf_charset(p))), reverse=True)
    patched = None
    try:
        if kind == "phone":
            from alfa_phone_stealth import _ensure_phone_font_chars

            patched = _ensure_phone_font_chars(str(work), NEED)
        else:
            patched = afe.ensure_alfa_font_chars(str(work), NEED, pool)
    except Exception as exc:
        logger.warning("alfa inject %s: %s", kind, exc)
        patched = None

    src = Path(patched) if patched and os.path.isfile(patched) else work
    # Best-effort: if full NEED failed mid-way, keep last inject progress from work dir temps
    if not patched:
        # Retry once starting from a й-bearing donor (SBP hard case)
        for p in pool:
            if "й" not in _pdf_charset(p):
                continue
            shutil.copy2(p, work)
            try:
                patched = afe.ensure_alfa_font_chars(str(work), NEED, pool)
            except Exception:
                patched = None
            if patched and os.path.isfile(patched):
                src = Path(patched)
                best = p
                break

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)
    miss = "".join(ch for ch in NEED if ch not in _pdf_charset(str(out)))
    logger.info(
        "alfa unlocked %s: %s (%d B) from %s n_hint=%d miss=%r",
        kind, out, out.stat().st_size, best, best_n, miss,
    )
    return out


def bake_alfa_channel(channel: str) -> bool:
    ch = channel.lower()
    if "phone" in ch:
        return bake_alfa_kind("phone") is not None
    if "card" in ch:
        return bake_alfa_kind("card") is not None
    if "sbp" in ch:
        return bake_alfa_kind("sbp") is not None
    ok = True
    for k in ("phone", "card", "sbp"):
        if bake_alfa_kind(k) is None:
            ok = False
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    if target == "all":
        raise SystemExit(0 if bake_alfa_channel("alfa") else 1)
    raise SystemExit(0 if bake_alfa_channel(target) else 1)
