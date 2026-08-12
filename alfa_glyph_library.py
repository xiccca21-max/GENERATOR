"""
Библиотека контуров Alfa SBP (Tahoma subset, upem=2048 как у Oracle).

Источник: корпус чеков → fallback Windows Tahoma (scale → 2048).
"""
from __future__ import annotations

import json
import logging
import os
import pickle
from copy import deepcopy
from io import BytesIO
from typing import Dict, Optional, Tuple

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import Glyph

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(_DIR, "alfa_glyph_library")
LIB_PICKLE = os.path.join(LIB_DIR, "glyphs.pkl")
LIB_META = os.path.join(LIB_DIR, "meta.json")
CORPUS_DEFAULT = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "альфа")
MASTER_TAHOMA = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "tahoma.ttf")
# Oracle BI Alfa subset Tahoma — всегда 2048.
TARGET_UPEM = 2048

_GLYPH_CACHE: Optional[Dict[int, dict]] = None


def _as_simple(font: TTFont, gname: str) -> Optional[Glyph]:
    from fontTools.misc.arrayTools import calcIntBounds
    from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates
    from fontTools.ttLib.tables.ttProgram import Program

    glyf = font["glyf"]
    g = deepcopy(glyf[gname])
    nc = getattr(g, "numberOfContours", 0)
    if nc > 0:
        return g
    if nc == 0:
        return None

    # composite → simple (Ё = E+dieresis и т.п.)
    comps = list(getattr(g, "components", []) or [])
    if not comps:
        return None
    out = Glyph()
    out.numberOfContours = 0
    out.coordinates = GlyphCoordinates([])
    out.flags = []
    out.endPtsOfContours = []
    prog = Program()
    prog.fromBytecode(b"")
    out.program = prog
    try:
        for comp in comps:
            cname, transform = comp.getComponentInfo()
            xx, xy, yx, yy, dx, dy = transform
            leaf = glyf[cname]
            # вложенный composite — рекурсия
            if getattr(leaf, "numberOfContours", 0) == -1:
                leaf = _as_simple(font, cname)
                if leaf is None:
                    return None
            elif getattr(leaf, "numberOfContours", 0) <= 0:
                continue
            else:
                leaf = deepcopy(leaf)
            start = len(out.coordinates)
            for i, (x, y) in enumerate(leaf.coordinates):
                nx = int(round(xx * x + xy * y + dx))
                ny = int(round(yx * x + yy * y + dy))
                out.coordinates.append((nx, ny))
                flags = list(getattr(leaf, "flags", []) or [])
                out.flags.append(flags[i] if i < len(flags) else 1)
            for ep in leaf.endPtsOfContours:
                out.endPtsOfContours.append(start + ep)
            out.numberOfContours += int(leaf.numberOfContours)
        if out.numberOfContours <= 0 or len(out.coordinates) == 0:
            return None
        out.xMin, out.yMin, out.xMax, out.yMax = calcIntBounds(out.coordinates)
        return out
    except Exception:
        return None


def _scale_glyph(g: Glyph, scale: float) -> Glyph:
    from fontTools.misc.arrayTools import calcIntBounds
    from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates

    out = deepcopy(g)
    if getattr(out, "numberOfContours", 0) <= 0:
        return out
    coords = GlyphCoordinates(
        [(x * scale, y * scale) for x, y in out.coordinates]
    )
    coords = coords.copy()  # ensure type
    # round to int
    rounded = GlyphCoordinates([(int(round(x)), int(round(y))) for x, y in coords])
    out.coordinates = rounded
    try:
        out.xMin, out.yMin, out.xMax, out.yMax = calcIntBounds(rounded)
    except Exception:
        xs = [p[0] for p in rounded]
        ys = [p[1] for p in rounded]
        out.xMin, out.xMax = min(xs), max(xs)
        out.yMin, out.yMax = min(ys), max(ys)
    return out


def _ingest_pdf(path: str, store: Dict[int, dict]) -> int:
    import fitz
    from alfa_orig_mode import AlfaOrigContext

    ctx = AlfaOrigContext()
    if not ctx.load(path) or not ctx.ff2_xref:
        return 0
    doc = fitz.open(path)
    try:
        ff2 = doc.xref_stream(ctx.ff2_xref)
    finally:
        doc.close()
    try:
        ft = TTFont(BytesIO(ff2))
    except Exception:
        return 0
    go = ft.getGlyphOrder()
    hmtx = ft["hmtx"].metrics if "hmtx" in ft else {}
    added = 0
    for cp, cid in ctx.uni_to_cid.items():
        if cp < 32:
            continue
        if cp in store and store[cp].get("from_corpus"):
            continue
        if cid < 0 or cid >= len(go):
            continue
        gname = go[cid]
        simple = _as_simple(ft, gname)
        if simple is None:
            continue
        aw, lsb = hmtx.get(gname, (0, 0))
        aw = int(aw or 0)
        lsb = int(lsb or 0)
        src_upem = int(ft["head"].unitsPerEm) or TARGET_UPEM
        if aw <= 0:
            pdf_w = int(ctx.widths.get(cid) or 500)
            aw = max(1, int(round(pdf_w * src_upem / 1000.0)))
        if src_upem != TARGET_UPEM and src_upem > 0:
            scale = TARGET_UPEM / float(src_upem)
            simple = _scale_glyph(simple, scale)
            aw = int(round(aw * scale))
            lsb = int(round(lsb * scale))
        store[cp] = {
            "glyph_pkl": pickle.dumps(simple, protocol=pickle.HIGHEST_PROTOCOL),
            "aw": aw,
            "lsb": lsb,
            "upem": TARGET_UPEM,
            "from_corpus": True,
            "source": os.path.basename(path),
        }
        added += 1
    return added


def _ingest_master_tahoma(store: Dict[int, dict]) -> int:
    if not os.path.isfile(MASTER_TAHOMA):
        return 0
    try:
        ft = TTFont(MASTER_TAHOMA)
    except Exception as exc:
        logger.warning("Alfa master Tahoma: %s", exc)
        return 0
    upem = int(ft["head"].unitsPerEm) or 2048
    scale = TARGET_UPEM / float(upem)
    cmap = ft.getBestCmap() or {}
    hmtx = ft["hmtx"].metrics
    added = 0
    for cp, gname in cmap.items():
        if cp < 32 or cp in store:
            continue
        simple = _as_simple(ft, gname)
        if simple is None:
            continue
        simple = _scale_glyph(simple, scale)
        aw_raw, lsb_raw = hmtx.get(gname, (500, 0))
        aw = int(round(int(aw_raw or 0) * scale))
        lsb = int(round(int(lsb_raw or 0) * scale))
        store[cp] = {
            "glyph_pkl": pickle.dumps(simple, protocol=pickle.HIGHEST_PROTOCOL),
            "aw": aw,
            "lsb": lsb,
            "upem": TARGET_UPEM,
            "from_corpus": False,
            "source": "tahoma.ttf",
        }
        added += 1
    return added


def build_library(*, force: bool = False) -> str:
    global _GLYPH_CACHE
    os.makedirs(LIB_DIR, exist_ok=True)
    if not force and os.path.isfile(LIB_PICKLE):
        return LIB_PICKLE

    store: Dict[int, dict] = {}
    corpus_n = 0
    if os.path.isdir(CORPUS_DEFAULT):
        for name in sorted(os.listdir(CORPUS_DEFAULT)):
            if not name.lower().endswith(".pdf"):
                continue
            corpus_n += _ingest_pdf(os.path.join(CORPUS_DEFAULT, name), store)

    master_n = _ingest_master_tahoma(store)
    with open(LIB_PICKLE, "wb") as fp:
        pickle.dump({"uni": store}, fp, protocol=pickle.HIGHEST_PROTOCOL)
    _GLYPH_CACHE = None
    meta = {
        "chars": len(store),
        "from_corpus_ingest": corpus_n,
        "from_tahoma": master_n,
        "corpus_chars": sum(1 for v in store.values() if v.get("from_corpus")),
    }
    with open(LIB_META, "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=2)
    logger.info("Alfa glyph library: %s", meta)
    return LIB_PICKLE


def ensure_library() -> None:
    build_library(force=False)


def _load() -> Dict[int, dict]:
    global _GLYPH_CACHE
    if _GLYPH_CACHE is not None:
        return _GLYPH_CACHE
    ensure_library()
    with open(LIB_PICKLE, "rb") as fp:
        payload = pickle.load(fp)
    _GLYPH_CACHE = payload.get("uni") or {}
    return _GLYPH_CACHE


def has_char(ch: str) -> bool:
    cp = 0x00A0 if ch == " " else ord(ch)
    return cp in _load()


def available_chars() -> set:
    return {chr(cp) for cp in _load() if cp >= 32}


def get_glyph(cp: int) -> Optional[Tuple[Glyph, int, int]]:
    """(simple Glyph, advanceWidth, lsb) в font units TARGET_UPEM=2048."""
    if cp == 0x20:
        cp = 0x00A0
    entry = _load().get(cp)
    if not entry:
        return None
    g = pickle.loads(entry["glyph_pkl"])
    aw = int(entry.get("aw") or 500)
    lsb = int(entry.get("lsb") or 0)
    stored_upem = int(entry.get("upem") or 0)
    # Старый pickle: master Tahoma был в 1000, corpus aw иногда PDF /W.
    y_ext = max(abs(getattr(g, "yMax", 0) or 0), abs(getattr(g, "yMin", 0) or 0))
    if stored_upem == TARGET_UPEM:
        pass
    elif stored_upem and stored_upem != TARGET_UPEM:
        scale = TARGET_UPEM / float(stored_upem)
        g = _scale_glyph(g, scale)
        aw = int(round(aw * scale))
        lsb = int(round(lsb * scale))
    elif y_ext > 0 and y_ext < 900:
        # Контуры ~1000-upem (старый Tahoma ingest).
        scale = TARGET_UPEM / 1000.0
        g = _scale_glyph(g, scale)
        aw = int(round(aw * scale))
        lsb = int(round(lsb * scale))
    else:
        # Контуры ~2048, но aw может быть PDF /W (~500).
        gw = abs((getattr(g, "xMax", 0) or 0) - (getattr(g, "xMin", 0) or 0))
        if gw > 0 and aw < gw * 0.85 and aw < 900:
            aw = max(1, int(round(aw * TARGET_UPEM / 1000.0)))
    return g, aw, lsb
