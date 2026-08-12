"""
Библиотека контуров Sber SBP (ArialMT), собранная из корпуса оригиналов.

Индекс: Unicode → glyph bundle + предпочтительные GID-слоты (как у T-Bank).
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
from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphCoordinates

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(_DIR, "glyph_library")
LIB_PICKLE = os.path.join(LIB_DIR, "sber_glyphs.pkl")
LIB_META = os.path.join(LIB_DIR, "sber_meta.json")
CORPUS_DEFAULT = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "сбер")
SHELL_TEMPLATE = os.path.join(_DIR, "templates", "S_sbp_original.pdf")
# Доп. источники глифов, которых нет в корпусе СБП (напр. заглавная Ю).
SUPPLEMENTARY_PDFS = [
    os.path.join(_DIR, "templates", "sber_original.pdf"),
]
# Previously refused in names; now empty — bot must accept any FIO (font-patch).
EXCLUDED_NAME_CHARS = frozenset()
_FULL_REQUIRED = (
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "0123456789"
)
# Backwards-compatible name for offline tooling.
_FULL_CYRILLIC = _FULL_REQUIRED
_SYSTEM_ARIAL_CANDIDATES = (
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "ARIAL.TTF"),
    "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)

_GLYPH_CACHE: Optional[dict] = None
_ARIAL_SUPPLEMENTED = False


def excluded_name_chars(text: str) -> Set[str]:
    return set(str(text or "")) & set(EXCLUDED_NAME_CHARS)


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


def _pick_arial_key(fonts: dict) -> Optional[str]:
    for key in fonts:
        if "Arial" in key:
            return key
    return next(iter(fonts), None) if fonts else None


def _load_font_from_pdf(path: str) -> Optional[Tuple[TTFont, Dict[int, int]]]:
    try:
        import fitz
        import tbank_unlock_template as tut

        doc = fitz.open(path)
        fm = tut._find_font_objects(doc)
        key = _pick_arial_key(fm)
        if not key:
            doc.close()
            return None
        meta = fm[key]
        if not meta.get("tounicode_xref"):
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
    except Exception as exc:
        logger.debug("load font %s: %s", path, exc)
        return None


def _ingest_font(
    ft: TTFont,
    uni_gid: Dict[int, int],
    store: Dict[int, dict],
    slot_counts: Dict[str, Dict[int, int]],
    *,
    from_corpus: bool,
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


def _ensure_glyph_coordinates(g: Glyph) -> Glyph:
    """fontTools recalcBounds/save need GlyphCoordinates, not a plain list."""
    coords = getattr(g, "coordinates", None)
    if coords is None:
        return g
    if isinstance(coords, GlyphCoordinates):
        return g
    try:
        g.coordinates = GlyphCoordinates(list(coords))
    except Exception:
        pass
    return g


def _find_system_arial() -> Optional[str]:
    for path in _SYSTEM_ARIAL_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def _as_simple_from_font(ft: TTFont, gname: str) -> Optional[Glyph]:
    """Composite → simple outline (без чужих component-имён в dest font)."""
    from copy import deepcopy as _dc

    glyf = ft["glyf"]
    if gname not in glyf:
        return None
    g = glyf[gname]
    nc = getattr(g, "numberOfContours", 0)
    if nc > 0:
        return _ensure_glyph_coordinates(_dc(g))
    if nc == 0:
        return None
    # Полная декомпозиция (Ё/Й = base + diacritic).
    try:
        from fontTools.pens.ttGlyphPen import TTGlyphPen
        from fontTools.pens.recordingPen import DecomposingRecordingPen

        glyph_set = ft.getGlyphSet()
        rec = DecomposingRecordingPen(glyph_set)
        glyph_set[gname].draw(rec)
        pen = TTGlyphPen(None)
        rec.replay(pen)
        out = pen.glyph()
        if getattr(out, "numberOfContours", 0) > 0:
            return _ensure_glyph_coordinates(out)
    except Exception:
        pass
    comps = list(getattr(g, "components", []) or [])
    if len(comps) == 1:
        info = comps[0].getComponentInfo()
        cname, transform = info[0], info[1]
        xx, xy, yx, yy, dx, dy = transform
        leaf = _dc(glyf[cname])
        if getattr(leaf, "numberOfContours", 0) <= 0:
            return None
        if (xx, xy, yx, yy, dx, dy) == (1, 0, 0, 1, 0, 0):
            return _ensure_glyph_coordinates(leaf)
        pts = []
        for x, y in leaf.coordinates:
            pts.append((
                int(round(xx * x + xy * y + dx)),
                int(round(yx * x + yy * y + dy)),
            ))
        leaf.coordinates = GlyphCoordinates(pts)
        try:
            leaf.recalcBounds(glyf)
        except Exception:
            pass
        return leaf
    return None


def _ingest_system_arial_missing(
    store: Dict[int, dict],
    slot_counts: Optional[Dict[str, Dict[int, int]]] = None,
) -> int:
    """Добирает обязательные буквы и цифры из системного Arial (upem=2048)."""
    need = [ord(ch) for ch in _FULL_REQUIRED if ord(ch) not in store]
    if not need:
        return 0
    path = _find_system_arial()
    if not path:
        logger.warning("Sber glyph library: system Arial not found, missing %s", len(need))
        return 0
    try:
        ft = TTFont(path)
    except Exception as exc:
        logger.warning("Sber glyph library: cannot open Arial: %s", exc)
        return 0
    cmap = None
    for table in ft["cmap"].tables:
        if table.platformID == 3 and table.platEncID in (1, 10):
            cmap = table.cmap
            break
    if not cmap:
        return 0
    h = ft["hmtx"].metrics
    added = 0
    for cp in need:
        gname = cmap.get(cp)
        if not gname:
            continue
        simple = _as_simple_from_font(ft, gname)
        if simple is None or getattr(simple, "numberOfContours", 0) <= 0:
            continue
        aw = int(h.get(gname, (0, 0))[0] or 0)
        # Синтетическое имя glyph{cp}: get_bundle мапит source_gid→слот назначения.
        syn = f"glyph{cp}"
        bundle = {syn: simple}
        _store_char_entry(
            store, cp, bundle,
            is_composite=False,
            component_gids=[],
            source_gid=cp,
            from_corpus=False,
            aw=aw,
        )
        if slot_counts is not None:
            ch_slots = slot_counts.setdefault(chr(cp), {})
            ch_slots[cp] = ch_slots.get(cp, 0) + 1
        added += 1
    if added:
        logger.info("Sber glyph library: +%d chars from system Arial", added)
    return added


def build_library(corpus_dir: Optional[str] = None, *, force: bool = False) -> str:
    global _GLYPH_CACHE
    os.makedirs(LIB_DIR, exist_ok=True)
    if not force and os.path.isfile(LIB_PICKLE):
        return LIB_PICKLE

    store: Dict[int, dict] = {}
    slot_counts: Dict[str, Dict[int, int]] = {}
    sources: Set[str] = set()

    corpus = corpus_dir or CORPUS_DEFAULT
    if os.path.isdir(corpus):
        for name in sorted(os.listdir(corpus)):
            if not name.lower().endswith(".pdf"):
                continue
            path = os.path.join(corpus, name)
            loaded = _load_font_from_pdf(path)
            if loaded:
                _ingest_font(loaded[0], loaded[1], store, slot_counts, from_corpus=True)
                sources.add(name)

    shell = _load_font_from_pdf(SHELL_TEMPLATE)
    if shell:
        _ingest_font(shell[0], shell[1], store, slot_counts, from_corpus=False)

    for path in SUPPLEMENTARY_PDFS:
        if not os.path.isfile(path):
            continue
        loaded = _load_font_from_pdf(path)
        if loaded:
            _ingest_font(loaded[0], loaded[1], store, slot_counts, from_corpus=False)

    _ingest_system_arial_missing(store, slot_counts)

    def _slot_prefs(counts: Dict[str, Dict[int, int]]) -> Dict[str, List[int]]:
        return {
            ch: [g for g, _ in sorted(ctr.items(), key=lambda x: (-x[1], x[0]))]
            for ch, ctr in counts.items()
        }

    payload = {
        "store": store,
        "slot_prefs": _slot_prefs(slot_counts),
    }
    with open(LIB_PICKLE, "wb") as fp:
        pickle.dump(payload, fp, protocol=pickle.HIGHEST_PROTOCOL)

    _GLYPH_CACHE = None
    chars = sorted(chr(k) for k in store if k > 0)
    meta = {
        "chars": len(chars),
        "corpus_files": len(sources),
        "charset": "".join(chars),
    }
    with open(LIB_META, "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=2)
    logger.info("Sber glyph library: %s chars from %s PDFs", meta["chars"], meta["corpus_files"])
    return LIB_PICKLE


def _persist_library(payload: dict) -> None:
    os.makedirs(LIB_DIR, exist_ok=True)
    with open(LIB_PICKLE, "wb") as fp:
        pickle.dump(payload, fp, protocol=pickle.HIGHEST_PROTOCOL)
    chars = sorted(chr(k) for k in payload.get("store", {}) if k > 0)
    meta = {
        "chars": len(chars),
        "corpus_files": payload.get("corpus_files_meta", "?"),
        "charset": "".join(chars),
    }
    # Preserve corpus_files count from previous meta if present.
    if os.path.isfile(LIB_META):
        try:
            with open(LIB_META, encoding="utf-8") as fp:
                old = json.load(fp)
            meta["corpus_files"] = old.get("corpus_files", meta["corpus_files"])
        except Exception:
            pass
    with open(LIB_META, "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=2)


def _supplement_loaded_library(lib: dict) -> dict:
    """Добирает кириллицу из Arial в уже собранный pickle (без полной пересборки)."""
    global _ARIAL_SUPPLEMENTED
    if _ARIAL_SUPPLEMENTED:
        return lib
    store = lib.get("store")
    if not isinstance(store, dict):
        _ARIAL_SUPPLEMENTED = True
        return lib
    slot_prefs = lib.setdefault("slot_prefs", {})
    slot_counts: Dict[str, Dict[int, int]] = {
        ch: {int(g): 1 for g in prefs}
        for ch, prefs in slot_prefs.items()
        if isinstance(prefs, list)
    }
    added = _ingest_system_arial_missing(store, slot_counts)
    _ARIAL_SUPPLEMENTED = True
    if added:
        lib["slot_prefs"] = {
            ch: [g for g, _ in sorted(ctr.items(), key=lambda x: (-x[1], x[0]))]
            for ch, ctr in slot_counts.items()
        }
        try:
            _persist_library(lib)
        except Exception as exc:
            logger.warning("Sber glyph library: persist supplement failed: %s", exc)
    return lib


def _load() -> dict:
    global _GLYPH_CACHE
    if _GLYPH_CACHE is not None:
        return _GLYPH_CACHE
    if not os.path.isfile(LIB_PICKLE):
        build_library()
    with open(LIB_PICKLE, "rb") as fp:
        _GLYPH_CACHE = pickle.load(fp)
    _GLYPH_CACHE = _supplement_loaded_library(_GLYPH_CACHE)
    return _GLYPH_CACHE


def ensure_library() -> None:
    _load()


def library_ready() -> bool:
    return os.path.isfile(LIB_PICKLE)


def merged_charset() -> Set[str]:
    lib = _load()
    return {chr(k) for k in lib.get("store", {}) if k > 0}


def _corpus_always_ok(ch: str) -> bool:
  """Пробел и ₽ есть в каждом shell Сбера (слот 0x20), в pickle их может не быть."""
  return ch in (" ", "\u00a0", "₽", "\u20bd")


SHELL_CHAR_FALLBACK = {
    # Empty when glyph library covers full Cyrillic (see shell_char_fallback_active).
    # Do NOT silently rewrite user FIO (э→е, Ё→Е, …).
}


def shell_char_fallback_active(corpus: Optional[Set[str]] = None) -> Dict[str, str]:
    """Подстановки только если исходной буквы нет в библиотеке, а замена есть."""
    if corpus is None:
        corpus = merged_charset()
    out: Dict[str, str] = {}
    for ch, repl in SHELL_CHAR_FALLBACK.items():
        if ch in corpus:
            continue
        r = repl
        if r not in corpus:
            if r.lower() in corpus:
                r = r.lower()
            elif r.upper() in corpus:
                r = r.upper()
        if r in corpus:
            out[ch] = r
    return out


def char_renderable(ch: str, corpus: Optional[Set[str]] = None) -> bool:
    """Символ есть в библиотеке или заменяется допустимым аналогом."""
    if _corpus_always_ok(ch) or ord(ch) < 0x20:
        return True
    if corpus is None:
        corpus = merged_charset()
    if ch in corpus:
        return True
    repl = SHELL_CHAR_FALLBACK.get(ch)
    if repl is None:
        return False
    active = shell_char_fallback_active(corpus)
    repl = active.get(ch)
    return repl is not None


def missing_unresolved(text: str) -> Set[str]:
    """Символы, которые нельзя отрисовать даже с fallback-заменой."""
    corpus = merged_charset()
    out: Set[str] = set()
    for ch in str(text or ""):
        if not char_renderable(ch, corpus):
            out.add(ch)
    return out


def covers_text(text: str) -> bool:
    """Все печатные символы строки есть в корпусной библиотеке (22 PDF Сбера)."""
    corpus = merged_charset()
    for ch in str(text or ""):
        if _corpus_always_ok(ch) or ord(ch) < 0x20:
            continue
        if ch not in corpus:
            return False
    return True


def missing_from_corpus(text: str) -> Set[str]:
    return missing_unresolved(text)


def slot_preferences() -> Dict[str, List[int]]:
    return _load().get("slot_prefs", {})


def get_bundle(cp: int, glyph_order: list, cid: int) -> Optional[Dict[str, Glyph]]:
    lib = _load()
    entry = lib["store"].get(cp)
    if not entry or "bundle_pkl" not in entry or cid >= len(glyph_order):
        return None
    raw: Dict[str, Glyph] = pickle.loads(entry["bundle_pkl"])
    src_gid = entry.get("source_gid", cid)
    out: Dict[str, Glyph] = {}
    for gname, g in raw.items():
        gidx = _glyph_name_to_gid(gname)
        if gidx is None:
            continue
        g = _ensure_glyph_coordinates(deepcopy(g))
        if gidx == src_gid:
            out[glyph_order[cid]] = g
        elif gidx < len(glyph_order):
            out[glyph_order[gidx]] = g
    return out or None


def get_bundle_component_gids(cp: int) -> List[int]:
    lib = _load()
    entry = lib["store"].get(cp)
    if not entry:
        return []
    return list(entry.get("component_gids") or [])


def get_entry_aw(cp: int) -> Optional[int]:
    entry = _load()["store"].get(cp)
    if not entry:
        return None
    aw = entry.get("aw")
    return int(aw) if aw else None


def get_glyph(cp: int) -> Optional[Glyph]:
    lib = _load()
    entry = lib["store"].get(cp)
    if not entry or "bundle_pkl" not in entry:
        return None
    raw = pickle.loads(entry["bundle_pkl"])
    return deepcopy(next(iter(raw.values())))
