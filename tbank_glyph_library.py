"""
Библиотека контуров T-Bank SBP (upem=1000), собранная из корпуса оригиналов.

Индекс: Unicode → composite/simple bundle (как в банке, без flatten).
"""
from __future__ import annotations

import json
import logging
import os
import pickle
from copy import deepcopy
from io import BytesIO
from typing import Dict, List, Optional, Set, Tuple

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import Glyph

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(_DIR, "glyph_library")
LIB_PICKLE = os.path.join(LIB_DIR, "glyphs.pkl")
LIB_META = os.path.join(LIB_DIR, "meta.json")
CORPUS_DEFAULT = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "т банк")
SHELL_TEMPLATE = os.path.join(_DIR, "templates", "T_sbp_original.pdf")
MASTER_REG = os.path.join(_DIR, "fonts", "TinkoffSans-TBank-Regular.ttf")
MASTER_MED = os.path.join(_DIR, "fonts", "TinkoffSans-TBank-Medium.ttf")

BANK_REG_GHOST: Set[int] = {4, 15, 16, 50, 57, 64, 87, 107, 129, 138, 178, 188, 221, 222}
BANK_MED_GHOST: Set[int] = {178}

_GLYPH_CACHE: Optional[dict] = None


def _glyph_name_to_gid(gname: str) -> Optional[int]:
    if not gname.startswith("glyph"):
        return None
    try:
        return int(gname[5:])
    except ValueError:
        return None


def _bundle_from_slot(g, gname: str, glyf, go: list) -> Tuple[Optional[dict], bool, List[int]]:
    nc = getattr(g, "numberOfContours", 0)
    if nc == 0:
        return None, False, []
    if nc == -1:
        g = deepcopy(g)
        if not getattr(g, "components", None):
            try:
                g.expand(glyf)
            except Exception:
                return None, False, []
        bundle: Dict[str, Glyph] = {gname: g}
        component_gids: List[int] = []
        for comp in g.components or []:
            cname = comp.glyphName
            try:
                cgid = go.index(cname)
            except ValueError:
                continue
            component_gids.append(cgid)
            cg = glyf[cname]
            if getattr(cg, "numberOfContours", 0) != 0:
                bundle[cname] = deepcopy(cg)
        return bundle, True, component_gids
    return {gname: deepcopy(g)}, False, []


def _entry_priority(entry: Optional[dict]) -> int:
    if not entry:
        return -1
    if entry.get("from_corpus") and entry.get("is_composite"):
        return 3
    if entry.get("from_corpus"):
        return 2
    if entry.get("is_composite"):
        return 1
    return 0


def _store_char_entry(
    store: Dict[int, dict],
    cp: int,
    bundle: dict,
    *,
    is_composite: bool,
    component_gids: List[int],
    source_gid: int,
    from_corpus: bool,
    aw: int,
) -> None:
    new_entry = {
        "bundle_pkl": pickle.dumps(bundle, protocol=pickle.HIGHEST_PROTOCOL),
        "is_composite": is_composite,
        "component_gids": component_gids,
        "source_gid": source_gid,
        "from_corpus": from_corpus,
        "aw": aw,
    }
    old = store.get(cp)
    if _entry_priority(new_entry) >= _entry_priority(old):
        store[cp] = new_entry


def _store_ghost(ghosts: Dict[int, dict], gid: int, g: Glyph) -> None:
    if getattr(g, "numberOfContours", 0) == 0:
        return
    ghosts[gid] = {"glyph_pkl": pickle.dumps(deepcopy(g), protocol=pickle.HIGHEST_PROTOCOL)}


def _load_font_from_pdf(path: str, font_key: str) -> Optional[Tuple[TTFont, Dict[int, int]]]:
    try:
        import fitz
        import tbank_unlock_template as tut

        doc = fitz.open(path)
        fm = tut._find_font_objects(doc)
        meta = fm.get(font_key)
        if not meta:
            doc.close()
            return None
        ff2 = doc.xref_stream(meta["fontfile_xref"])
        sub = tut._parse_subset_tounicode(
            doc.xref_stream(meta["tounicode_xref"]).decode("latin1", "replace")
        )
        doc.close()
        ft = TTFont(BytesIO(ff2))
        uni_gid = {u: c for c, u in sub.items()}
        return ft, uni_gid
    except Exception:
        return None


def _ingest_font(
    ft: TTFont,
    uni_gid: Dict[int, int],
    store: Dict[int, dict],
    slot_counts: Dict[str, Dict[int, int]],
    ghosts: Dict[int, dict],
    *,
    from_corpus: bool,
    is_medium: bool = False,
) -> None:
    glyf = ft["glyf"]
    go = ft.getGlyphOrder()
    h = ft["hmtx"].metrics

    for cp, gid in uni_gid.items():
        if cp < 0x20 or gid >= len(go):
            continue
        gname = go[gid]
        bundle, is_comp, comp_gids = _bundle_from_slot(glyf[gname], gname, glyf, go)
        if not bundle:
            continue
        aw = h.get(gname, (0, 0))[0]
        _store_char_entry(
            store, cp, bundle,
            is_composite=is_comp,
            component_gids=comp_gids,
            source_gid=gid,
            from_corpus=from_corpus,
            aw=aw,
        )
        ch_slots = slot_counts.setdefault(chr(cp), {})
        ch_slots[gid] = ch_slots.get(gid, 0) + 1
        for cgid in comp_gids:
            if cgid < len(go):
                _store_ghost(ghosts, cgid, glyf[go[cgid]])

    for gid in (BANK_MED_GHOST if is_medium else BANK_REG_GHOST):
        if gid >= len(go):
            continue
        _store_ghost(ghosts, gid, glyf[go[gid]])


def _ingest_master_fallback(path: str, char_to_gid: dict, store: Dict[int, dict]) -> None:
    if not os.path.isfile(path):
        return
    try:
        from tbank_sbp_stealth import TBANK_CHAR_TO_GID_REG

        ft = TTFont(path)
        glyf = ft["glyf"]
        go = ft.getGlyphOrder()
        h = ft["hmtx"].metrics
        upem = ft["head"].unitsPerEm
        scale = 1000 / upem if upem else 1.0
        for ch, gid in char_to_gid.items():
            cp = ord(ch)
            if cp in store:
                continue
            if gid >= len(go):
                continue
            gname = go[gid]
            bundle, is_comp, comp_gids = _bundle_from_slot(glyf[gname], gname, glyf, go)
            if not bundle:
                continue
            aw = int(h.get(gname, (0, 0))[0] * scale)
            _store_char_entry(
                store, cp, bundle,
                is_composite=is_comp,
                component_gids=comp_gids,
                source_gid=gid,
                from_corpus=False,
                aw=aw,
            )
    except Exception as exc:
        logger.warning("master fallback %s: %s", path, exc)


def build_library(corpus_dir: Optional[str] = None, *, force: bool = False) -> str:
    global _GLYPH_CACHE
    os.makedirs(LIB_DIR, exist_ok=True)
    if not force and os.path.isfile(LIB_PICKLE):
        return LIB_PICKLE

    from tbank_sbp_stealth import TBANK_CHAR_TO_GID_MED, TBANK_CHAR_TO_GID_REG, _all_corpus_font_paths

    reg_store: Dict[int, dict] = {}
    med_store: Dict[int, dict] = {}
    reg_slots: Dict[str, Dict[int, int]] = {}
    med_slots: Dict[str, Dict[int, int]] = {}
    reg_ghosts: Dict[int, dict] = {}
    med_ghosts: Dict[int, dict] = {}

    pdf_paths: List[str] = []
    seen: Set[str] = set()

    def _add_pdf(path: str) -> None:
        ap = os.path.abspath(path)
        if ap in seen or not os.path.isfile(ap):
            return
        if not ap.lower().endswith(".pdf"):
            return
        seen.add(ap)
        pdf_paths.append(ap)

    corpus = corpus_dir or CORPUS_DEFAULT
    if os.path.isdir(corpus):
        for name in sorted(os.listdir(corpus)):
            _add_pdf(os.path.join(corpus, name))
    # All live T-Bank method templates + shells — full letter/digit coverage
    # must not depend on a single donor subset.
    for path in _all_corpus_font_paths():
        _add_pdf(path)
    templates = os.path.join(_DIR, "templates")
    if os.path.isdir(templates):
        for name in sorted(os.listdir(templates)):
            if name.startswith("T_") and name.lower().endswith(".pdf"):
                _add_pdf(os.path.join(templates, name))

    for path in pdf_paths:
        for fk, store, slots, ghosts, is_med in (
            ("TinkoffSans-Regular", reg_store, reg_slots, reg_ghosts, False),
            ("TinkoffSans-Medium", med_store, med_slots, med_ghosts, True),
        ):
            loaded = _load_font_from_pdf(path, fk)
            if loaded:
                _ingest_font(
                    loaded[0], loaded[1], store, slots, ghosts,
                    from_corpus=True, is_medium=is_med,
                )

    shell = _load_font_from_pdf(SHELL_TEMPLATE, "TinkoffSans-Regular")
    if shell:
        _ingest_font(
            shell[0], shell[1], reg_store, reg_slots, reg_ghosts,
            from_corpus=False, is_medium=False,
        )
    shell_m = _load_font_from_pdf(SHELL_TEMPLATE, "TinkoffSans-Medium")
    if shell_m:
        _ingest_font(
            shell_m[0], shell_m[1], med_store, med_slots, med_ghosts,
            from_corpus=False, is_medium=True,
        )

    _ingest_master_fallback(MASTER_REG, TBANK_CHAR_TO_GID_REG, reg_store)
    _ingest_master_fallback(MASTER_MED, TBANK_CHAR_TO_GID_MED, med_store)

    for gid, entry in reg_ghosts.items():
        reg_store[-(gid + 1)] = {"ghost_gid": gid, **entry}
    for gid, entry in med_ghosts.items():
        med_store[-(gid + 1)] = {"ghost_gid": gid, **entry}

    def _slot_prefs(counts: Dict[str, Dict[int, int]]) -> Dict[str, List[int]]:
        return {ch: [g for g, _ in sorted(ctr.items(), key=lambda x: (-x[1], x[0]))] for ch, ctr in counts.items()}

    payload = {
        "reg": reg_store,
        "med": med_store,
        "slot_prefs_reg": _slot_prefs(reg_slots),
        "slot_prefs_med": _slot_prefs(med_slots),
    }
    with open(LIB_PICKLE, "wb") as fp:
        pickle.dump(payload, fp, protocol=pickle.HIGHEST_PROTOCOL)

    _GLYPH_CACHE = None
    meta = {
        "reg_chars": len([k for k in reg_store if k > 0]),
        "med_chars": len([k for k in med_store if k > 0]),
        "ghosts_reg": len(reg_ghosts),
        "ghosts_med": len(med_ghosts),
        "pdf_sources": len(pdf_paths),
        "composite_chars": len([k for k, v in reg_store.items() if k > 0 and v.get("is_composite")]),
    }
    with open(LIB_META, "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=2)
    logger.info("glyph library: %s", meta)
    return LIB_PICKLE


def _load() -> dict:
    global _GLYPH_CACHE
    if _GLYPH_CACHE is not None:
        return _GLYPH_CACHE
    if not os.path.isfile(LIB_PICKLE):
        build_library()
    with open(LIB_PICKLE, "rb") as fp:
        _GLYPH_CACHE = pickle.load(fp)
    return _GLYPH_CACHE


def ensure_library() -> None:
    _load()


def slot_preferences(is_medium: bool = False) -> Dict[str, List[int]]:
    lib = _load()
    key = "slot_prefs_med" if is_medium else "slot_prefs_reg"
    return lib.get(key, {})


def get_bundle(cp: int, glyph_order: list, cid: int, *, is_medium: bool = False) -> Optional[Dict[str, Glyph]]:
    """Return a bank-atlas contour bundle for ``cp`` remapped onto ``cid``.

    Prefer corpus-harvested OpenPDF contours. Master-fallback outlines are a
    different family and trip Proton ``K-TBANK-GLYPH-ATLAS-001``.
    """
    lib = _load()
    store = lib["med" if is_medium else "reg"]
    entry = store.get(cp)
    if not entry or "bundle_pkl" not in entry or cid >= len(glyph_order) or cid < 0:
        return None
    # Atlas-safe: only contours harvested from real T-Bank PDFs.
    if not entry.get("from_corpus", False):
        return None
    raw: Dict[str, Glyph] = pickle.loads(entry["bundle_pkl"])
    if not raw:
        return None
    src_gid = int(entry.get("source_gid", cid))
    out: Dict[str, Glyph] = {}
    root_name = glyph_order[cid]
    root_placed = False
    for gname, g in raw.items():
        gidx = _glyph_name_to_gid(gname)
        if gidx is not None and gidx == src_gid:
            out[root_name] = deepcopy(g)
            root_placed = True
            break
    if not root_placed:
        first = next(iter(raw.values()))
        out[root_name] = deepcopy(first)
        root_placed = True
    for gname, g in raw.items():
        gidx = _glyph_name_to_gid(gname)
        if gidx is None or gidx == src_gid:
            continue
        if 0 <= gidx < len(glyph_order):
            out[glyph_order[gidx]] = deepcopy(g)
    return out or None


def get_bundle_component_gids(cp: int, *, is_medium: bool = False) -> List[int]:
    lib = _load()
    store = lib["med" if is_medium else "reg"]
    entry = store.get(cp)
    if not entry:
        return []
    return list(entry.get("component_gids") or [])


def get_glyph(cp: int, *, is_medium: bool = False) -> Optional[Glyph]:
    lib = _load()
    store = lib["med" if is_medium else "reg"]
    entry = store.get(cp)
    if not entry or "bundle_pkl" not in entry:
        return None
    raw = pickle.loads(entry["bundle_pkl"])
    return deepcopy(next(iter(raw.values())))


def get_ghost_bundle(gid: int, glyph_order: list, *, is_medium: bool = False) -> Optional[Dict[str, Glyph]]:
    lib = _load()
    store = lib["med" if is_medium else "reg"]
    entry = store.get(-(gid + 1))
    if not entry or gid >= len(glyph_order):
        return None
    return {glyph_order[gid]: pickle.loads(entry["glyph_pkl"])}


def library_ready() -> bool:
    return os.path.isfile(LIB_PICKLE)
