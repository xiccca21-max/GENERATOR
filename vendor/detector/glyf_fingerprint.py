"""
Glyf / pixel fingerprint checks for T-Bank receipts.

Reference: detector/glyf_reference.json (build via tools/build_glyf_reference.py).
Generator self-test: assert_glyf_ok(pdf_bytes).
"""

from __future__ import annotations

import hashlib
import json
import re
import zlib
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

try:
    import fitz
except ImportError:
    fitz = None

try:
    from fontTools.ttLib import TTFont
except ImportError:
    TTFont = None

from .pdf_forensics import _BF1_RE, _BF2_RE, _BFCHAR_RE, _BFRANGE_RE
from .structure import find_streams, is_content_stream

_REF_PATH = Path(__file__).with_name("glyf_reference.json")

_OBJ_HDR = re.compile(rb"(\d+)\s+0\s+obj")
_FONT_RES = re.compile(rb"/(F\d+)\s+(\d+)\s+0\s+R")

ANCHOR_Y = {
    "total_label": 106.61,
    "commission_zero": 203.22,
}

_TEMPLATE_CHARS = {
    "F1": frozenset(
        "0123456789 "
        "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
        "абвгдежзийклмнопрстуфхцчшщъыьэюя"
        "ёЁ+-.,()@№"
    ),
    "F2": frozenset("0123456789 Итого"),
    "F3": frozenset("i"),
}


@dataclass
class GlyfFlag:
    code: str
    detail: str


@dataclass
class GlyfResult:
    flags: list[GlyfFlag] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _load_reference() -> dict:
    if not _REF_PATH.is_file():
        return {"version": 0, "anchors": {}, "glyphs": {}}
    return json.loads(_REF_PATH.read_text(encoding="utf-8"))


def _obj_spans(pdf: bytes) -> dict[int, bytes]:
    spans: dict[int, bytes] = {}
    for m in _OBJ_HDR.finditer(pdf):
        num = int(m.group(1))
        end = pdf.find(b"endobj", m.start())
        if end > 0:
            spans[num] = pdf[m.start() : end]
    return spans


def _font_resources(pdf: bytes) -> dict[str, int]:
    return {m.group(1).decode(): int(m.group(2)) for m in _FONT_RES.finditer(pdf)}


def _stream_bytes(obj_blob: bytes, spans: dict[int, bytes]) -> bytes | None:
    m = re.search(rb"/Length\s+(\d+)", obj_blob)
    pos = obj_blob.find(b"stream")
    if pos < 0:
        return None
    cs = pos + 6
    if obj_blob[cs : cs + 2] == b"\r\n":
        cs += 2
    elif obj_blob[cs : cs + 1] == b"\n":
        cs += 1
    es = obj_blob.find(b"endstream", cs)
    raw = obj_blob[cs:es].rstrip(b"\r\n")
    if b"/Filter" in obj_blob[:pos] and b"/FlateDecode" in obj_blob[:pos]:
        try:
            return zlib.decompress(raw)
        except Exception:
            return None
    return raw


def _parse_cmap_blob(blob: bytes) -> dict[int, str]:
    cid_map: dict[int, str] = {}
    for m in _BFCHAR_RE.finditer(blob):
        for hm in _BF1_RE.finditer(m.group(1)):
            try:
                cid = int(hm.group(1), 16)
                u = bytes.fromhex(hm.group(2).decode()[:4]).decode("utf-16-be", "ignore")
                if u:
                    cid_map[cid] = u
            except Exception:
                pass
    for m in _BFRANGE_RE.finditer(blob):
        for hm in _BF2_RE.finditer(m.group(1)):
            try:
                lo, hi = int(hm.group(1), 16), int(hm.group(2), 16)
                base = int(hm.group(3).decode(), 16)
                for i, cid in enumerate(range(lo, hi + 1)):
                    cid_map[cid] = chr(base + i)
            except Exception:
                pass
    return cid_map


def _cids_from_tj_token(tok: bytes) -> list[int]:
    if tok.startswith(b"<") and tok.endswith(b">"):
        hexs = tok[1:-1].decode("ascii", "ignore")
        return [int(hexs[i : i + 4], 16) for i in range(0, len(hexs) - 3, 4)]
    if not (tok.startswith(b"(") and tok.endswith(b")")):
        return []
    inner = tok[1:-1]
    raw: list[int] = []
    i = 0
    while i < len(inner):
        c = inner[i : i + 1]
        if c == b"\\" and i + 1 < len(inner):
            n = inner[i + 1 : i + 2]
            if n in b"0123456789":
                j = i + 1
                octal = b""
                while j < len(inner) and len(octal) < 3 and inner[j : j + 1] in b"0123456789":
                    octal += inner[j : j + 1]
                    j += 1
                raw.append(int(octal, 8) & 0xFF)
                i = j
            elif n == b"n":
                raw.append(10)
                i += 2
            elif n == b"r":
                raw.append(13)
                i += 2
            elif n == b"t":
                raw.append(9)
                i += 2
            elif n == b"b":
                raw.append(8)
                i += 2
            elif n == b"f":
                raw.append(12)
                i += 2
            else:
                raw.append(ord(n))
                i += 2
        else:
            raw.append(inner[i])
            i += 1
    cids: list[int] = []
    for k in range(0, len(raw) - 1, 2):
        cids.append(raw[k] * 256 + raw[k + 1])
    if len(raw) % 2 == 1 and raw:
        cids.append(raw[-1])
    return cids


def _font_cid_maps(pdf: bytes) -> dict[str, dict[int, str]]:
    """Per F1/F2: CID → Unicode from /ToUnicode on Type0 font object."""
    spans = _obj_spans(pdf)
    res = _font_resources(pdf)
    out: dict[str, dict[int, str]] = {}
    for font_key, num in res.items():
        if font_key not in ("F1", "F2"):
            continue
        blob = spans.get(num, b"")
        tu = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", blob)
        if not tu:
            continue
        tu_blob = spans.get(int(tu.group(1)), b"")
        dec = _stream_bytes(tu_blob, spans)
        if dec:
            out[font_key] = _parse_cmap_blob(dec)
    return out


def _extract_fontfile2(pdf: bytes, font_key: str) -> bytes | None:
    spans = _obj_spans(pdf)
    res = _font_resources(pdf)
    num = res.get(font_key)
    if not num or num not in spans:
        return None
    blob = spans[num]
    dnum = re.search(rb"/DescendantFonts\s*\[\s*(\d+)", blob)
    if dnum:
        blob = spans[int(dnum.group(1))]
    fd = re.search(rb"/FontDescriptor\s+(\d+)\s+0\s+R", blob)
    if not fd:
        return None
    fd_blob = spans[int(fd.group(1))]
    ff2 = re.search(rb"/FontFile2\s+(\d+)\s+0\s+R", fd_blob)
    if not ff2:
        return None
    ff2obj = spans[int(ff2.group(1))]
    dec = _stream_bytes(ff2obj, spans)
    return dec


def _glyf_md5_by_gid(ttf: bytes, gid: int) -> str | None:
    if TTFont is None:
        return None
    try:
        tt = TTFont(BytesIO(ttf))
        order = tt.getGlyphOrder()
        if gid < 0 or gid >= len(order):
            return None
        g = tt["glyf"][order[gid]]
        data = g.compile(tt["glyf"]) if hasattr(g, "compile") else b""
        if not data:
            return None
        return hashlib.md5(data).hexdigest()[:12]
    except Exception:
        return None


def _collect_used_by_font(pdf: bytes) -> dict[str, set[int]]:
    used: dict[str, set[int]] = {}
    tj_re = re.compile(rb"(\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f]+>)\s*TJ?", re.S)
    for _, dec in find_streams(pdf):
        if not is_content_stream(dec):
            continue
        current = ""
        for line in dec.split(b"\n"):
            tf = re.search(rb"/(F\d+)\s+[\d.]+\s+Tf", line)
            if tf:
                current = tf.group(1).decode()
            if not current or current == "F3":
                continue
            for m in tj_re.finditer(line):
                for cid in _cids_from_tj_token(m.group(1)):
                    used.setdefault(current, set()).add(cid)
    return used


def _pixel_hash_at_y(pdf_bytes: bytes, target_y: float, text_hint: str | None,
                     scale: int = 10) -> tuple[str | None, str | None]:
    if not fitz:
        return None, None
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    found_hash = None
    found_text = None
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for sp in line["spans"]:
                oy = round(sp["origin"][1], 2)
                if abs(oy - target_y) > 0.6:
                    continue
                text = sp["text"].strip()
                if text_hint and text != text_hint:
                    continue
                rect = fitz.Rect(sp["bbox"]) + (-1, -1, 1, 1)
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, alpha=False)
                found_hash = hashlib.md5(pix.samples).hexdigest()[:12]
                found_text = text
    doc.close()
    return found_hash, found_text


def _is_tbank_receipt(pdf_bytes: bytes) -> bool:
    if not fitz:
        return False
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
    except Exception:
        return False
    return "Итого" in text and ("Перевод" in text or "перевод" in text)


def run_glyf_checks(pdf_bytes: bytes) -> GlyfResult:
    res = GlyfResult()
    if not fitz or not _is_tbank_receipt(pdf_bytes):
        return res

    ref = _load_reference()
    if not ref.get("anchors"):
        res.stats["glyf_ref_missing"] = True
        return res

    res.stats["glyf_ref_version"] = ref.get("version")

    for anchor_id, y in ANCHOR_Y.items():
        spec = anchors.get(anchor_id) if (anchors := ref.get("anchors", {})) else None
        if not spec:
            continue
        hint = spec.get("text_hint")
        pix, text = _pixel_hash_at_y(pdf_bytes, y, hint)
        res.stats[f"anchor_{anchor_id}_pix"] = pix
        if pix is None:
            continue
        allowed = set(spec.get("pix_hashes", []))
        if allowed and pix not in allowed:
            res.flags.append(GlyfFlag(
                f"GLYPH_PIX_{anchor_id.upper()}",
                f"контур «{text or hint}» не совпадает с настоящими чеками Т-Банка "
                f"(отпечаток {pix})",
            ))

    cid_maps = _font_cid_maps(pdf_bytes)
    used = _collect_used_by_font(pdf_bytes)
    glyphs_ref = ref.get("glyphs", {})

    for font_key in ("F1", "F2", "F3"):
        font_ref = glyphs_ref.get(font_key, {})
        if not font_ref:
            continue
        ttf = _extract_fontfile2(pdf_bytes, font_key)
        cmap = cid_maps.get(font_key, {})
        if not ttf or not cmap:
            continue

        chars_seen: set[str] = set()
        for cid in used.get(font_key, set()):
            ch = cmap.get(cid)
            if not ch or ch in chars_seen:
                continue
            if ch not in _TEMPLATE_CHARS[font_key]:
                continue
            entry = font_ref.get(ch)
            if not entry:
                continue
            chars_seen.add(ch)
            gm = _glyf_md5_by_gid(ttf, cid)
            if not gm:
                continue
            allowed_g = set(entry.get("glyf_md5", []))
            if allowed_g and gm not in allowed_g:
                res.flags.append(GlyfFlag(
                    f"GLYPH_TTF_{font_key}",
                    f"контур символа {ch!r} в {font_key} не совпадает с банковским "
                    f"OpenPDF (glyf {gm})",
                ))
                break

    return res


def assert_glyf_ok(pdf_bytes: bytes) -> None:
    result = run_glyf_checks(pdf_bytes)
    if result.flags:
        raise ValueError("; ".join(f.detail for f in result.flags))


def glyf_to_log_dict(result: GlyfResult) -> dict:
    return {
        "flags": [{"code": f.code, "detail": f.detail} for f in result.flags],
        "stats": result.stats,
    }


# ── Corpus builder API (used by tools/build_glyf_reference.py) ───────────────

def collect_fingerprints_from_pdf(pdf_bytes: bytes) -> dict:
    """Extract anchor pix + per-char glyf from one genuine PDF."""
    out: dict = {"anchors": {}, "glyphs": {"F1": {}, "F2": {}, "F3": {}}}
    for anchor_id, y in ANCHOR_Y.items():
        hint = "Итого" if anchor_id == "total_label" else "0"
        pix, text = _pixel_hash_at_y(pdf_bytes, y, hint)
        if pix:
            out["anchors"][anchor_id] = {"pix": pix, "text": text}

    cid_maps = _font_cid_maps(pdf_bytes)
    used = _collect_used_by_font(pdf_bytes)
    for font_key in ("F1", "F2", "F3"):
        ttf = _extract_fontfile2(pdf_bytes, font_key)
        cmap = cid_maps.get(font_key, {})
        if not ttf or not cmap:
            continue
        for cid in used.get(font_key, set()):
            ch = cmap.get(cid)
            if not ch or ch not in _TEMPLATE_CHARS[font_key]:
                continue
            gm = _glyf_md5_by_gid(ttf, cid)
            if not gm:
                continue
            slot = out["glyphs"][font_key].setdefault(ch, set())
            slot.add(gm)
    return out


def merge_fingerprint_corpus(samples: list[dict]) -> dict:
    anchors_acc: dict[str, dict] = {}
    glyphs_acc: dict[str, dict[str, set[str]]] = {"F1": {}, "F2": {}, "F3": {}}

    for sample in samples:
        for aid, data in sample.get("anchors", {}).items():
            anchors_acc.setdefault(aid, {"pix_hashes": set(), "text_hint": None})
            if data.get("pix"):
                anchors_acc[aid]["pix_hashes"].add(data["pix"])
            if aid == "total_label":
                anchors_acc[aid]["text_hint"] = "Итого"
            elif aid == "commission_zero":
                anchors_acc[aid]["text_hint"] = "0"

        for fk in ("F1", "F2", "F3"):
            for ch, hashes in sample.get("glyphs", {}).get(fk, {}).items():
                glyphs_acc[fk].setdefault(ch, set()).update(hashes)

    return {
        "version": 1,
        "anchors": {
            k: {"pix_hashes": sorted(v["pix_hashes"]), "text_hint": v["text_hint"]}
            for k, v in anchors_acc.items()
        },
        "glyphs": {
            fk: {ch: {"glyf_md5": sorted(hs)} for ch, hs in chars.items()}
            for fk, chars in glyphs_acc.items()
        },
    }
