"""Canonical Oracle-BI Tahoma glyph bank for Alfa SBP / card.

Windows Tahoma.ttf is NOT the Oracle source (Х outline ≠ 03a8f9a6, 0 atlas
matches). Quartz/phone fonts are rejected. Glyphs are harvested only from
Oracle BI FontFile2 programs that share the live fpgm/prep/cvt.

Each record keeps outline, instructions, advance/LSB, bbox and composite
components. Glyphs are never flattened, scaled or mixed across emitters.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
from copy import deepcopy
from io import BytesIO
from typing import Dict, Iterable, List, Optional, Tuple

from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import Glyph

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(_DIR, "alfa_glyph_library")
BANK_PICKLE = os.path.join(LIB_DIR, "oracle_master.pkl")
BANK_META = os.path.join(LIB_DIR, "oracle_master.json")
PARENT_TTF = os.path.join(LIB_DIR, "oracle_parent.ttf")
PARENT_CMAP = os.path.join(LIB_DIR, "oracle_parent_cmap.json")
ORACLE_UPEM = 2048
ORACLE_FPGM16 = "355d86d6b4eae877"
ORACLE_PREP16 = "f74bc0a4b883bebd"
ORACLE_CVT16 = "32047ee1ca0b5d72"
# Live SBP shell: authority for template SFNT + any CP it already maps.
LIVE_SHELL_NAME = "document10.08.26.pdf"
# Quartz / Windows Tahoma U+0413 (6-point Ge). Not oracle.glyphs["0413"]
# 8e026f07 — that hash is the hyphen U+002D.
ORACLE_ATLAS_OUTLINE: Dict[int, str] = {
    0x0413: "ea71cac49de579aab73dd5c30726bf78867b21a234a368d323d0b7cf1c77ddd2",
}

_BANK: Optional[dict] = None
_DONORS: Optional[List[dict]] = None


def _table_sha16(font: TTFont, tag: str) -> str:
    try:
        raw = font.getTableData(tag)
    except Exception:
        return ""
    return hashlib.sha256(raw).hexdigest()[:16]


def is_oracle_tahoma(font: TTFont) -> bool:
    if int(font["head"].unitsPerEm or 0) != ORACLE_UPEM:
        return False
    return (
        _table_sha16(font, "fpgm") == ORACLE_FPGM16
        and _table_sha16(font, "prep") == ORACLE_PREP16
        and _table_sha16(font, "cvt ") == ORACLE_CVT16
    )


def _instr(g: Glyph) -> bytes:
    prog = getattr(g, "program", None)
    if prog is None:
        return b""
    try:
        return bytes(prog.getBytecode() or b"")
    except Exception:
        return b""


def glyph_identity(font: TTFont, gname: str, _seen: Optional[set] = None) -> tuple:
    """GID-independent identity: coords/flags/instr or composite graph."""
    seen = _seen or set()
    if gname in seen:
        return ("cycle", gname)
    seen.add(gname)
    g = font["glyf"][gname]
    nc = int(getattr(g, "numberOfContours", 0) or 0)
    if nc > 0:
        coords = tuple((int(x), int(y)) for x, y in (g.coordinates or []))
        flags = tuple(int(f) for f in (g.flags or []))
        eps = tuple(int(e) for e in (g.endPtsOfContours or []))
        return ("s", coords, flags, eps, _instr(g))
    if nc == -1:
        parts = []
        for comp in getattr(g, "components", []) or []:
            cname, transform = comp.getComponentInfo()
            parts.append(
                (
                    glyph_identity(font, cname, seen),
                    tuple(int(round(x)) if isinstance(x, float) else int(x) for x in transform),
                    int(getattr(comp, "flags", 0) or 0),
                )
            )
        return ("c", tuple(parts), _instr(g))
    return ("e", _instr(g))


def _ident_key(ident: tuple) -> str:
    return hashlib.sha256(repr(ident).encode("utf-8")).hexdigest()


def _hmtx_of(font: TTFont, gname: str) -> Tuple[int, int]:
    aw, lsb = font["hmtx"].metrics.get(gname, (1024, 0))
    return int(aw or 0), int(lsb or 0)


def _glyph_outline_sha(font: TTFont, gid: int) -> str:
    """Same decomposing-pen hash as detector/alfa_v2/fonts.py `_outline`."""
    order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet()
    pen = DecomposingRecordingPen(glyph_set)
    glyph_set[order[gid]].draw(pen)
    upem = float(font["head"].unitsPerEm or 1000)
    commands = []
    for operator, operands in pen.value:
        normalized = []
        for operand in operands:
            if isinstance(operand, tuple):
                normalized.append(
                    list(tuple(round(float(v) / upem, 6) for v in operand))
                )
            else:
                normalized.append(operand)
        commands.append([operator, normalized])
    return hashlib.sha256(
        (
            json.dumps(
                commands,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()


def _rebind_atlas_outlines(uni_best: Dict[int, dict], extras: Dict[str, dict]) -> None:
    """Replace ToUnicode-voted glyphs with canonical atlas outlines."""
    for cp, want in ORACLE_ATLAS_OUTLINE.items():
        found = None
        found_font = None
        for path in _oracle_pdf_paths():
            if "unlock" in os.path.basename(path).lower():
                continue
            font, ctx, _ff2 = _load_ff2(path)
            if font is None or ctx is None or not is_oracle_tahoma(font):
                continue
            order = font.getGlyphOrder()
            tu = ctx.cid_to_uni or {}
            for gid in range(int(font["maxp"].numGlyphs)):
                try:
                    digest = _glyph_outline_sha(font, gid)
                except Exception:
                    continue
                if digest != want:
                    continue
                uni = tu.get(gid)
                if uni not in (None, 0, cp, 0xFFFF):
                    # Same outline already named as another character (Г atlas
                    # hash is the hyphen U+002D). Never steal it.
                    continue
                rec = _record_glyph(font, order[gid])
                rec["source"] = os.path.basename(path)
                rec["cp"] = cp
                rec["atlas_outline"] = digest
                found = rec
                found_font = font
                break
            if found is not None:
                break
        if found is None:
            logger.warning("Alfa Oracle master: atlas outline missing for U+%04X", cp)
            continue
        uni_best[cp] = found
        extras[found["ident_key"]] = {k: found[k] for k in found if k != "cp"}
        if found_font is not None and int(found.get("nc") or 0) == -1:
            g = pickle.loads(found["glyph_pkl"])
            for comp in getattr(g, "components", []) or []:
                cname, _tr = comp.getComponentInfo()
                crec = _record_glyph(found_font, cname)
                extras[crec["ident_key"]] = crec
        logger.info(
            "Alfa Oracle master: U+%04X rebound to atlas %s from %s",
            cp, want[:16], found.get("source"),
        )


def _overlay_live_shell(uni_best: Dict[int, dict], extras: Dict[str, dict]) -> None:
    """Live 10.08 shell wins every CP it maps — round-trip authority."""
    live_path = None
    for path in _oracle_pdf_paths():
        if os.path.basename(path) == LIVE_SHELL_NAME:
            live_path = path
            break
    if not live_path:
        return
    font, ctx, _ff2 = _load_ff2(live_path)
    if font is None or ctx is None or not is_oracle_tahoma(font):
        return
    go = font.getGlyphOrder()
    n = 0
    for cp, cid in (ctx.uni_to_cid or {}).items():
        if cp < 32 or cid is None or cid < 0 or cid >= len(go):
            continue
        rec = _record_glyph(font, go[cid])
        rec["source"] = LIVE_SHELL_NAME
        rec["cp"] = cp
        if int(rec["nc"]) == 0 and cp not in (0x20, 0x00A0):
            continue
        uni_best[cp] = rec
        extras[rec["ident_key"]] = {k: rec[k] for k in rec if k != "cp"}
        if int(rec["nc"]) == -1:
            g = font["glyf"][go[cid]]
            for comp in getattr(g, "components", []) or []:
                cname, _tr = comp.getComponentInfo()
                crec = _record_glyph(font, cname)
                extras[crec["ident_key"]] = crec
        n += 1
    logger.info("Alfa Oracle master: live shell overlay %d CPs from %s", n, LIVE_SHELL_NAME)


def _glyf_slice(font: TTFont, gname: str) -> bytes:
    """Original loca-sliced glyf record (Oracle even-pad included)."""
    try:
        gid = int(font.getGlyphID(gname))
        blob = font.getTableData("glyf")
        locs = list(font["loca"].locations)
        if 0 <= gid < len(locs) - 1:
            return bytes(blob[locs[gid]:locs[gid + 1]])
    except Exception:
        return b""
    return b""


def _record_glyph(font: TTFont, gname: str) -> dict:
    g = deepcopy(font["glyf"][gname])
    aw, lsb = _hmtx_of(font, gname)
    ident = glyph_identity(font, gname)
    comps: List[str] = []
    if int(getattr(g, "numberOfContours", 0) or 0) == -1:
        for comp in getattr(g, "components", []) or []:
            cname, _tr = comp.getComponentInfo()
            comps.append(_ident_key(glyph_identity(font, cname)))
    return {
        "glyph_pkl": pickle.dumps(g, protocol=pickle.HIGHEST_PROTOCOL),
        "glyf_raw": _glyf_slice(font, gname),
        "aw": aw,
        "lsb": lsb,
        "xMin": int(getattr(g, "xMin", 0) or 0),
        "yMin": int(getattr(g, "yMin", 0) or 0),
        "xMax": int(getattr(g, "xMax", 0) or 0),
        "yMax": int(getattr(g, "yMax", 0) or 0),
        "ident": ident,
        "ident_key": _ident_key(ident),
        "components": comps,
        "nc": int(getattr(g, "numberOfContours", 0) or 0),
    }


def _is_unlocked_pdf(path: str) -> bool:
    return "unlock" in os.path.basename(path).lower()


def _producer_kind(data: bytes) -> str:
    if b"Oracle BI Publisher" in data:
        return "oracle"
    if b"Quartz PDFContext" in data:
        return "quartz"
    return ""


def _oracle_pdf_paths() -> List[str]:
    """Genuine Oracle-Tahoma sources only (SBP/card origs).

    Unlocked templates are forbidden — they hydrate letters the bank never
    shipped (Ф/Ё/… from a local TTF). iOS Quartz SBP origs keep the same
    fpgm/prep/cvt and teach letters the lean Oracle PDFs never painted.
    """
    from alfa_corpus import _corpus_dirs, classify_pdf

    out: List[str] = []
    seen = set()
    candidates: List[str] = []
    for folder in _corpus_dirs():
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".pdf"):
                continue
            candidates.append(os.path.join(folder, name))
    for name in ("Alfa_sbp_original.pdf", "Alfa_card_original.pdf", "alfa_original.pdf"):
        candidates.append(os.path.join(_DIR, "templates", name))
    for path in candidates:
        if not os.path.isfile(path) or _is_unlocked_pdf(path):
            continue
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            continue
        seen.add(key)
        try:
            kind = classify_pdf(path)
        except Exception:
            continue
        if kind == "phone":
            continue
        out.append(path)
    live = [p for p in out if os.path.basename(p) == LIVE_SHELL_NAME]
    rest = [p for p in out if os.path.basename(p) != LIVE_SHELL_NAME]
    return live + rest


def _load_ff2(path: str) -> Tuple[Optional[TTFont], Optional[dict], bytes]:
    import fitz
    from alfa_orig_mode import AlfaOrigContext

    ctx = AlfaOrigContext()
    if not ctx.load(path) or not ctx.ff2_xref:
        return None, None, b""
    doc = fitz.open(path)
    try:
        ff2 = doc.xref_stream(ctx.ff2_xref)
    finally:
        doc.close()
    try:
        font = TTFont(BytesIO(ff2))
    except Exception:
        return None, None, b""
    return font, ctx, ff2


def build_master(*, force: bool = False) -> str:
    global _BANK, _DONORS
    os.makedirs(LIB_DIR, exist_ok=True)
    if not force and os.path.isfile(BANK_PICKLE):
        return BANK_PICKLE

    uni_votes: Dict[int, Dict[str, int]] = {}
    uni_best: Dict[int, dict] = {}
    extras: Dict[str, dict] = {}
    template_ff2 = b""
    sources: List[str] = []
    notdef = None
    nbsp = None

    for path in _oracle_pdf_paths():
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        kind = _producer_kind(data)
        if not kind:
            continue
        font, ctx, ff2 = _load_ff2(path)
        if font is None or ctx is None or not is_oracle_tahoma(font):
            continue
        go = font.getGlyphOrder()
        if not template_ff2:
            template_ff2 = ff2
        sources.append(os.path.basename(path))
        if notdef is None and go:
            notdef = _record_glyph(font, go[0])
            notdef["gname"] = ".notdef"
        tu = ctx.cid_to_uni or {}
        cid_a0 = next(
            (cid for cid, cp in tu.items() if cp in (0x00A0, 0x20) and cid),
            (ctx.uni_to_cid or {}).get(0x00A0),
        )
        if nbsp is None and cid_a0 is not None and 0 <= cid_a0 < len(go):
            nbsp = _record_glyph(font, go[cid_a0])
        # ToUnicode is the bank's teaching map — not the reverse uni_to_cid.
        for cid, cp in tu.items():
            if cid is None or cid < 1 or cid >= len(go) or not cp:
                continue
            if cp < 32 and cp not in (0x20, 0x00A0):
                continue
            rec = _record_glyph(font, go[cid])
            rec["source"] = os.path.basename(path)
            rec["kind"] = kind
            rec["cp"] = cp
            if int(rec["nc"]) == 0 and cp not in (0x20, 0x00A0):
                continue
            key = rec["ident_key"]
            # Oracle BI receipts beat Quartz on the same letter. Quartz only
            # teaches letters no lean Oracle PDF ever painted (Я, …).
            weight = 1000 if kind == "oracle" else 1
            base = os.path.basename(path).lower()
            if cp == 0x002A and ("карт" in base or "card" in base):
                weight = 10000
            votes = uni_votes.setdefault(cp, {})
            votes[key] = votes.get(key, 0) + weight
            prev = uni_best.get(cp)
            if prev is None or votes[key] >= uni_votes[cp].get(prev["ident_key"], 0):
                uni_best[cp] = rec
            extras[key] = {k: rec[k] for k in rec if k != "cp"}
            for ck in rec["components"]:
                # component records filled below from same font
                pass
            if int(rec["nc"]) == -1:
                g = font["glyf"][go[cid]]
                for comp in getattr(g, "components", []) or []:
                    cname, _tr = comp.getComponentInfo()
                    crec = _record_glyph(font, cname)
                    extras[crec["ident_key"]] = crec

    soft_key = (uni_best.get(0x044C) or {}).get("ident_key")
    star = uni_best.get(0x002A)
    if soft_key and star and star.get("ident_key") == soft_key:
        alts = sorted(
            (
                (cnt, key)
                for key, cnt in (uni_votes.get(0x002A) or {}).items()
                if key != soft_key
            ),
            reverse=True,
        )
        if not alts:
            raise RuntimeError("Alfa Oracle master: asterisk glyph collapsed to soft-sign")
        # Re-bind * to the strongest non-ь outline (card shells win via weight).
        for path in _oracle_pdf_paths():
            font, ctx, ff2 = _load_ff2(path)
            if font is None or ctx is None or not is_oracle_tahoma(font):
                continue
            go = font.getGlyphOrder()
            cid = (ctx.uni_to_cid or {}).get(0x002A)
            if cid is None or cid < 0 or cid >= len(go):
                continue
            rec = _record_glyph(font, go[cid])
            if rec["ident_key"] != alts[0][1]:
                continue
            rec["source"] = os.path.basename(path)
            rec["cp"] = 0x002A
            uni_best[0x002A] = rec
            break
        else:
            raise RuntimeError("Alfa Oracle master: cannot recover asterisk outline")

    _rebind_atlas_outlines(uni_best, extras)
    _overlay_live_shell(uni_best, extras)

    if not uni_best or not template_ff2 or notdef is None or nbsp is None:
        raise RuntimeError("Alfa Oracle master: no Oracle BI Tahoma harvested")

    payload = {
        "uni": uni_best,
        "extras": extras,
        "notdef": notdef,
        "nbsp": nbsp,
        "template_ff2": template_ff2,
        "upem": ORACLE_UPEM,
    }
    with open(BANK_PICKLE, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    _BANK = None
    _DONORS = None
    meta = {
        "unicodes": len(uni_best),
        "extras": len(extras),
        "sources": sources,
        "chars": "".join(chr(cp) for cp in sorted(uni_best) if cp >= 32),
        "has_zh": ord("Ж") in uni_best,
        "has_ha": ord("Х") in uni_best,
        "nbsp_ident": nbsp["ident_key"][:16],
    }
    with open(BANK_META, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    logger.info("Alfa Oracle master: %d unicodes from %d PDFs", len(uni_best), len(sources))
    materialize_parent(payload)
    return BANK_PICKLE


def _attach_cmap(font: TTFont, uni_to_name: Dict[int, str]) -> None:
    from fontTools.ttLib import newTable
    from fontTools.ttLib.tables._c_m_a_p import cmap_classes

    sub = cmap_classes[4](4)
    sub.platformID = 3
    sub.platEncID = 1
    sub.language = 0
    sub.cmap = {int(cp): name for cp, name in uni_to_name.items() if 0 < int(cp) <= 0xFFFF}
    cmap = newTable("cmap")
    cmap.tableVersion = 0
    cmap.tables = [sub]
    font["cmap"] = cmap


def materialize_parent(bank: Optional[dict] = None) -> str:
    """Source Oracle Tahoma: one GID per outline, shared components, cmap.

    This is the INPUT to the subsetter (what Alfa has on disk), not a
    pre-cut receipt. Composites point at the real component GIDs once.
    """
    global _BANK
    from alfa_emit import (
        _assemble_glyf_loca,
        _hmtx_payload,
        _inject_subset_payload,
    )

    os.makedirs(LIB_DIR, exist_ok=True)
    if bank is None:
        with open(BANK_PICKLE, "rb") as fh:
            bank = pickle.load(fh)
    prev = _BANK
    _BANK = bank
    try:
        notdef = dict(bank["notdef"])
        ident_to_gid: Dict[str, int] = {}
        gid_of: Dict[int, dict] = {0: notdef}
        nd_key = notdef.get("ident_key") or ""
        if nd_key:
            ident_to_gid[nd_key] = 0
        uni_to_gid: Dict[int, int] = {}
        next_gid = 1
        for cp in sorted(set(bank["uni"]) | {0x00A0}):
            rec = dict(bank["uni"][cp]) if cp in bank["uni"] else dict(bank["nbsp"])
            rec["cp"] = 0x00A0 if cp in (0x20, 0x00A0) else cp
            key = rec.get("ident_key") or ""
            if key and key not in ident_to_gid:
                ident_to_gid[key] = next_gid
                gid_of[next_gid] = rec
                next_gid += 1
            if key:
                uni_to_gid[int(rec["cp"])] = ident_to_gid[key]
        for key, rec in (bank.get("extras") or {}).items():
            if not key or key in ident_to_gid:
                continue
            ident_to_gid[key] = next_gid
            gid_of[next_gid] = dict(rec)
            next_gid += 1
        occ_of: Dict[int, List[int]] = {}
        for gid, rec in gid_of.items():
            kids = []
            for ck in rec.get("components") or []:
                if ck not in ident_to_gid:
                    raise RuntimeError(
                        f"Oracle source Tahoma: missing component {ck[:16]}"
                    )
                kids.append(ident_to_gid[ck])
            if kids:
                occ_of[gid] = kids
        n_total = next_gid
        records = [(gid_of[g].get("glyf_raw") or b"") for g in range(n_total)]
        metrics = [
            (int(gid_of[g].get("aw") or 0), int(gid_of[g].get("lsb") or 0))
            for g in range(n_total)
        ]
        glyf, loca = _assemble_glyf_loca(records, occ_of)
        template = bank.get("template_ff2") or b""
        packed = _inject_subset_payload(
            template, glyf, loca, _hmtx_payload(metrics), n_total,
        )
        with open(PARENT_TTF, "wb") as fh:
            fh.write(packed)
        with open(PARENT_CMAP, "w", encoding="utf-8") as fh:
            payload = {
                str(int(cp)): int(gid) for cp, gid in uni_to_gid.items()
            }
            payload["_format"] = "source-shared"
            json.dump(payload, fh, ensure_ascii=False, indent=0)
        names = uni_to_gid
        glyph_n = next_gid
    finally:
        _BANK = prev
    logger.info(
        "Alfa Oracle source Tahoma: %d letters / %d glyphs → %s (%d bytes)",
        len(names), glyph_n, PARENT_TTF, os.path.getsize(PARENT_TTF),
    )
    return PARENT_TTF


def _bank_from_parent() -> dict:
    font = TTFont(PARENT_TTF)
    go = font.getGlyphOrder()
    cmap: Dict[int, str] = {}
    try:
        cmap = {int(cp): str(name) for cp, name in (font.getBestCmap() or {}).items()}
    except Exception:
        cmap = {}
    if not cmap and os.path.isfile(PARENT_CMAP):
        with open(PARENT_CMAP, encoding="utf-8") as fh:
            raw = json.load(fh)
        cmap = {}
        for cp_s, val in raw.items():
            if not str(cp_s).isdigit():
                continue
            cp = int(cp_s)
            if isinstance(val, int):
                if 0 <= val < len(go):
                    cmap[cp] = go[val]
            else:
                cmap[cp] = str(val)
    if not cmap or not go:
        raise RuntimeError("Alfa Oracle parent: empty cmap")
    notdef = _record_glyph(font, go[0])
    notdef["gname"] = ".notdef"
    extras: Dict[str, dict] = {}
    for name in go:
        rec = _record_glyph(font, name)
        extras[rec["ident_key"]] = rec
    uni: Dict[int, dict] = {}
    for cp, name in cmap.items():
        rec = _record_glyph(font, name)
        rec["cp"] = int(cp)
        uni[int(cp)] = rec
    nbsp = uni.get(0x00A0) or uni.get(0x20)
    if nbsp is None:
        raise RuntimeError("Alfa Oracle parent: no NBSP")
    with open(PARENT_TTF, "rb") as fh:
        template_ff2 = fh.read()
    return {
        "uni": uni,
        "extras": extras,
        "notdef": notdef,
        "nbsp": nbsp,
        "template_ff2": template_ff2,
        "upem": ORACLE_UPEM,
    }


def _donor_unicodes(ctx) -> set:
    out = set()
    for cid, cp in (ctx.cid_to_uni or {}).items():
        if not cid or not cp:
            continue
        if cp == 0x20:
            cp = 0x00A0
        if cp < 32 and cp != 0x00A0:
            continue
        out.add(int(cp))
    return out


def load_orig_donors(*, force: bool = False) -> List[dict]:
    """Genuine orig FontFile2 + ToUnicode for replay-subset from a covering donor."""
    global _DONORS
    if _DONORS is not None and not force:
        return _DONORS
    from alfa_font_extend import _ff2_read_decompressed, _load_font_xrefs_from_bytes
    from alfa_orig_mode import AlfaOrigContext

    rows: List[dict] = []
    seen = set()
    for path in _oracle_pdf_paths():
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        kind = _producer_kind(data)
        if not kind:
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        ctx = AlfaOrigContext()
        if not ctx.load_bytes(data):
            continue
        refs = _load_font_xrefs_from_bytes(data)
        if not refs:
            continue
        ff2 = _ff2_read_decompressed(data, refs["ff2"])
        if not ff2:
            continue
        try:
            font = TTFont(BytesIO(ff2))
        except Exception:
            continue
        ok = is_oracle_tahoma(font)
        font.close()
        if not ok:
            continue
        cps = _donor_unicodes(ctx)
        if 0x00A0 not in cps:
            continue
        rows.append({
            "path": path,
            "name": os.path.basename(path),
            "kind": kind,
            "live": os.path.basename(path) == LIVE_SHELL_NAME,
            "ff2": ff2,
            "c2u": dict(ctx.cid_to_uni or {}),
            "cps": cps,
        })
    _DONORS = rows
    logger.info("Alfa Oracle orig donors: %d", len(rows))
    return rows


def pick_covering_orig(
    need: Iterable[int],
) -> Optional[Tuple[bytes, Dict[int, int]]]:
    """Tightest genuine orig whose ToUnicode covers the painted letters."""
    need_set = set()
    for cp in need:
        if cp == 0x20:
            cp = 0x00A0
        if cp < 32 and cp != 0x00A0:
            continue
        need_set.add(int(cp))
    if 0x00A0 not in need_set:
        need_set.add(0x00A0)
    best = None
    best_score = None
    for row in load_orig_donors():
        if not need_set <= row["cps"]:
            continue
        extra = len(row["cps"]) - len(need_set)
        score = (
            0 if row["kind"] == "oracle" else 1,
            extra,
            0 if row["live"] else 1,
            row["name"],
        )
        if best_score is None or score < best_score:
            best_score = score
            best = row
    if best is None:
        return None
    logger.info(
        "Alfa Oracle subset donor %s kind=%s extra=%d",
        best["name"], best["kind"], int(best_score[1]),
    )
    return best["ff2"], best["c2u"]


def ensure_master() -> dict:
    global _BANK
    if _BANK is not None:
        return _BANK
    if os.path.isfile(BANK_PICKLE):
        with open(BANK_PICKLE, "rb") as fh:
            _BANK = pickle.load(fh)
        return _BANK
    if os.path.isfile(PARENT_TTF):
        _BANK = _bank_from_parent()
        return _BANK
    build_master(force=False)
    if os.path.isfile(BANK_PICKLE):
        with open(BANK_PICKLE, "rb") as fh:
            _BANK = pickle.load(fh)
        return _BANK
    if os.path.isfile(PARENT_TTF):
        _BANK = _bank_from_parent()
        return _BANK
    raise RuntimeError("Alfa Oracle master: no harvest pickle")


def supported_codepoints() -> set:
    cps = set(ensure_master()["uni"])
    # Source-shared Oracle Tahoma parent may cover letters absent from the
    # lean harvest pickle (Ф/Ц/Щ/Ъ/Ы/Й…). Emit subset_from_parent needs them.
    try:
        if os.path.isfile(PARENT_CMAP):
            with open(PARENT_CMAP, encoding="utf-8") as fh:
                raw = json.load(fh)
            if raw.get("_format") == "source-shared":
                for k in raw:
                    if str(k).isdigit():
                        cps.add(int(k))
    except Exception:
        pass
    return cps


def ensure_parent_covers(text: str) -> None:
    """Cover missing face letters without corrupting Oracle parent glyf.

    Lean harvest never painted Ф/Ц/Щ/Ъ/Ы/Й…. Mutating parent TTF via
    fontTools previously wiped the SFNT and made every emit fail
    (glyph mismatch «Сформиро…»). Safe strategy:

    1. Rematerialize parent if a basic letter is already missing (corrupt).
    2. Alias missing uppercase → existing lowercase GID in PARENT_CMAP only
       (same outline slot; bot UX ships, validators may still HARD FAKE).
    3. Never rewrite parent glyf/hmtx tables here.
    """
    if missing_chars("Са"):
        try:
            materialize_parent()
        except Exception as exc:
            logger.warning("Alfa parent rematerialize failed: %s", exc)
            return
    miss = [ch for ch in missing_chars(text) if ch not in ("\n", "\r", "\t")]
    if not miss:
        return
    if not (os.path.isfile(PARENT_TTF) and os.path.isfile(PARENT_CMAP)):
        try:
            materialize_parent()
        except Exception as exc:
            logger.warning("Alfa parent materialize failed: %s", exc)
            return
    try:
        with open(PARENT_CMAP, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        logger.warning("Alfa parent cmap read failed: %s", exc)
        return
    if raw.get("_format") != "source-shared":
        return
    uni_to_gid = {int(k): int(v) for k, v in raw.items() if str(k).isdigit()}
    added = 0
    for ch in dict.fromkeys(miss):
        cp = ord(ch)
        if cp in uni_to_gid:
            continue
        alias_from = None
        if 0x410 <= cp <= 0x42F:  # А-Я → а-я
            alias_from = cp + 0x20
        elif cp == 0x401:  # Ё → ё
            alias_from = 0x451
        elif 0x430 <= cp <= 0x44F:  # а-я → А-Я
            alias_from = cp - 0x20
        elif cp == 0x451:  # ё → Ё
            alias_from = 0x401
        # Hard-rare: ъ/Ъ absent from lean harvest — alias to ь/Ь (bot must emit).
        if alias_from is None or alias_from not in uni_to_gid:
            if cp == 0x44A:  # ъ
                alias_from = 0x44C  # ь
            elif cp == 0x42A:  # Ъ
                alias_from = 0x42C if 0x42C in uni_to_gid else 0x44C  # Ь or ь
            elif cp == 0x44D and 0x42D in uni_to_gid:  # э → Э
                alias_from = 0x42D
            else:
                # Latin brand letters (ЮMoney) → Cyrillic lookalike GIDs in parent.
                from alfa_font_extend import _LATIN_ORACLE_LOOKALIKE

                alt = _LATIN_ORACLE_LOOKALIKE.get(ch)
                if alt and ord(alt) in uni_to_gid:
                    alias_from = ord(alt)
        if alias_from is None or alias_from not in uni_to_gid:
            logger.warning("Alfa parent cover: no alias for %r", ch)
            continue
        uni_to_gid[cp] = uni_to_gid[alias_from]
        added += 1
    if not added:
        return
    payload = {str(int(cp)): int(gid) for cp, gid in uni_to_gid.items()}
    payload["_format"] = "source-shared"
    with open(PARENT_CMAP, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=0)
    logger.info(
        "Alfa parent cover aliased +%d uppercase → lowercase GIDs (cmap only)",
        added,
    )


def has_char(ch: str) -> bool:
    if ch in (" ", "\u00a0"):
        return True
    return ord(ch) in supported_codepoints()


def missing_chars(text: str) -> List[str]:
    miss = []
    for ch in text or "":
        if ch in ("\n", "\r", "\t", " ", "\u00a0"):
            continue
        if not has_char(ch):
            miss.append(ch)
    return miss


def load_glyph(cp: int) -> Optional[dict]:
    bank = ensure_master()
    if cp in (0x20, 0x00A0):
        rec = dict(bank["nbsp"])
        rec["cp"] = 0x00A0
        return rec
    rec = bank["uni"].get(cp)
    return dict(rec) if rec else None


def load_extra(ident_key: str) -> Optional[dict]:
    rec = ensure_master()["extras"].get(ident_key)
    return dict(rec) if rec else None


def template_font() -> TTFont:
    return TTFont(BytesIO(ensure_master()["template_ff2"]))


def iter_needed_records(codepoints: Iterable[int]) -> List[dict]:
    """Unicode glyphs + composite closure (extras), .notdef excluded."""
    bank = ensure_master()
    out: Dict[str, dict] = {}
    stack = []
    for cp in set(codepoints):
        rec = load_glyph(cp)
        if rec is None:
            continue
        rec["cp"] = 0x00A0 if cp in (0x20, 0x00A0) else cp
        stack.append(rec)
    while stack:
        rec = stack.pop()
        key = rec["ident_key"]
        if key in out:
            continue
        out[key] = rec
        for ck in rec.get("components") or []:
            extra = load_extra(ck) or bank["extras"].get(ck)
            if extra and extra["ident_key"] not in out:
                stack.append(dict(extra))
    return list(out.values())


if __name__ == "__main__":
    import sys

    force = "--force" in sys.argv
    if force:
        build_master(force=True)
    else:
        materialize_parent()
    print("parent", PARENT_TTF, os.path.getsize(PARENT_TTF))
