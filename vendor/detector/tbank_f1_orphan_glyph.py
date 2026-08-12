"""A-TBANK-F1-ORPHAN-SIMPLE-GLYPH-001 — nonempty simple F1 glyph outside subset closure.

Native Jasper/OpenPDF SBP (bank5=00117) F1 TinkoffSans-Regular embeds only
glyphs reachable from ToUnicode/Identity via composite closure (+ .notdef).
A nonempty *simple* glyph outside that closure is reassembly residue → HARD.

GID 0 (.notdef) is excluded. No shadow mode / SHA blacklist / used-CID skip.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from .corpus_profiles import CHANNEL_SBP, detect_receipt_channel
from .sbp_cipher import extract_sbp_opid
from .structure import content_stream_bytes, find_streams, is_content_stream
from .tbank_jasper_profile import PROFILE_ID, claims_confirmed_tbank_profile
from .tbank_reassembly_family_v3 import (
    _cids_from_operand,
    _obj_spans,
    _parse_tounicode,
    _parse_w_array,
    _resolve_ref,
    _stream_payload,
    resolve_font_graph,
)

RULE_ID = "A-TBANK-F1-ORPHAN-SIMPLE-GLYPH-001"
CODE = "TBANK_F1_ORPHAN_SIMPLE_GLYPH"

_SUBJECT_MARKERS = (b"/reports/IB/Receipt", b"IB/Receipt")
_CREATOR_PREFIX = (
    "JasperReports Library version "
    "6.20.3-415f9428cffdb6805c6f85bbb29ebaf18813a2ab"
)
_PRODUCER_EXACT = "OpenPDF 1.3.30.jaspersoft.2"
_BANK5 = "00117"

_F1_STATIC_TABLES = {
    b"cvt ": 86,
    b"fpgm": 481,
    b"head": 54,
    b"hhea": 36,
    b"hmtx": 1902,
    b"maxp": 32,
    b"prep": 450,
}
_F1_NUM_GLYPHS = 476
_F1_NUM_H_METRICS = 475
_F1_UPEM = 1000

_MORE_COMPONENTS = 0x0020
_ARG_1_AND_2_ARE_WORDS = 0x0001
_WE_HAVE_A_SCALE = 0x0008
_WE_HAVE_AN_X_AND_Y_SCALE = 0x0040
_WE_HAVE_A_TWO_BY_TWO = 0x0080

_TF_RE = re.compile(rb"/([A-Za-z][A-Za-z0-9]*)\s+[\d.]+\s+Tf")
_SHOW_RE = re.compile(
    rb"("
    rb"\((?:\\.|[^\\()])*\)"
    rb"|<(?:[0-9A-Fa-f]+)>"
    rb"|\[(?:[^\[\]]|\[[^\]]*\])*\]"
    rb")\s*(Tj|TJ|'|\")",
    re.S,
)
_STR_IN_ARRAY_RE = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f]+>")
_FONT_NAME_RE = re.compile(rb"/(F\d+)\s+(\d+)\s+0\s+R")


@dataclass
class HardFlag:
    code: str
    detail: str
    rule_id: str = RULE_ID
    tier: str = "A"
    group: str = "font_cmap_glyph"


@dataclass
class OrphanGlyphResult:
    flags: list[HardFlag] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    orphan_telemetry: list[dict[str, Any]] = field(default_factory=list)


def _pdf_text(pdf: bytes) -> str:
    try:
        import fitz
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = "".join(p.get_text() for p in doc)
        doc.close()
        return text or ""
    except Exception:
        return ""


def _meta(pdf: bytes) -> tuple[str, str]:
    try:
        import fitz
        doc = fitz.open(stream=pdf, filetype="pdf")
        meta = doc.metadata or {}
        doc.close()
        return str(meta.get("producer") or ""), str(meta.get("creator") or "")
    except Exception:
        return "", ""


def _is_tbank_receipt(pdf: bytes, text: str) -> bool:
    if any(m in pdf for m in _SUBJECT_MARKERS):
        return True
    if not text:
        return b"Receipt" in pdf or "Перевод".encode("utf-8") in pdf
    markers = ("Перевод", "Квитанция", "Итого", "Статус")
    return sum(1 for m in markers if m in text) >= 2


def _sbp_bank5(text: str) -> str:
    opid = extract_sbp_opid(text) or ""
    if len(opid) >= 27:
        return opid[22:27]
    return ""


def profile_gate_f1_orphan(
    pdf: bytes,
    *,
    text: str = "",
    producer: str = "",
    creator: str = "",
) -> tuple[bool, dict[str, Any]]:
    if not producer and not creator:
        producer, creator = _meta(pdf)
    if not text:
        text = _pdf_text(pdf)
    channel = detect_receipt_channel(text)
    flat = " ".join(text.split()).lower()
    if any(m in flat for m in (
        "идентификатор операции",
        "id операции в сбп",
        "id операции сбп",
        "номер операции в сбп",
        "сбп id",
        "системы быстрых платежей",
    )):
        channel = CHANNEL_SBP
    subject_ok = any(m in pdf for m in _SUBJECT_MARKERS)
    producer_ok = (producer or "").strip() == _PRODUCER_EXACT
    creator_ok = (creator or "").startswith(_CREATOR_PREFIX)
    bank5 = _sbp_bank5(text)
    confirmed = claims_confirmed_tbank_profile(
        pdf, producer=producer, creator=creator,
    )
    receipt_ok = _is_tbank_receipt(pdf, text)
    ok = bool(
        receipt_ok
        and channel == CHANNEL_SBP
        and subject_ok
        and producer_ok
        and creator_ok
        and bank5 == _BANK5
        and confirmed
    )
    return ok, {
        "profile_gate": ok,
        "channel": channel,
        "subject_ok": subject_ok,
        "producer_ok": producer_ok,
        "creator_ok": creator_ok,
        "bank5": bank5,
        "bank5_ok": bank5 == _BANK5,
        "confirmed": confirmed,
        "receipt_ok": receipt_ok,
        "profile_id": PROFILE_ID,
        "producer": producer,
        "creator": creator,
    }


def _font_dict_from_resources(res_blob: bytes, spans: dict[int, bytes]) -> dict[str, int]:
    out: dict[str, int] = {}
    font_blob = res_blob
    font_ref = _resolve_ref(res_blob, b"/Font")
    if font_ref is not None and font_ref in spans:
        font_blob = spans[font_ref]
    fm = re.search(rb"/Font\s*<<((?:[^>]|>(?!>))*)>>", res_blob, re.S)
    if fm:
        font_blob = fm.group(1)
    for m in _FONT_NAME_RE.finditer(font_blob):
        out[m.group(1).decode("ascii")] = int(m.group(2))
    return out


def _xobject_map(res_blob: bytes, spans: dict[int, bytes]) -> dict[str, int]:
    out: dict[str, int] = {}
    xo = res_blob
    xref = _resolve_ref(res_blob, b"/XObject")
    if xref is not None and xref in spans:
        xo = spans[xref]
    xm = re.search(rb"/XObject\s*<<((?:[^>]|>(?!>))*)>>", res_blob, re.S)
    if xm:
        xo = xm.group(1)
    for m in re.finditer(rb"/([A-Za-z][A-Za-z0-9]+)\s+(\d+)\s+0\s+R", xo):
        out[m.group(1).decode("ascii")] = int(m.group(2))
    return out


def _collect_content_scopes(pdf: bytes) -> list[tuple[bytes, dict[str, int]]]:
    """(content_bytes, font_name→obj) for page + Form XObject tree."""
    spans = _obj_spans(pdf)
    scopes: list[tuple[bytes, dict[str, int]]] = []
    seen_forms: set[int] = set()

    page_blob = b""
    for blob in spans.values():
        if re.search(rb"/Type\s*/Page\b", blob) and b"/Parent" in blob:
            page_blob = blob
            break
    if not page_blob:
        for blob in spans.values():
            if re.search(rb"/Type\s*/Page\b", blob):
                page_blob = blob
                break

    def resources_for(obj_blob: bytes) -> bytes:
        res_ref = _resolve_ref(obj_blob, b"/Resources")
        if res_ref is not None and res_ref in spans:
            return spans[res_ref]
        rm = re.search(rb"/Resources\s*<<", obj_blob)
        if rm:
            return obj_blob
        return obj_blob

    def content_of(obj_blob: bytes) -> bytes:
        # /Contents n 0 R or array
        refs = re.findall(rb"/Contents\s*(\d+)\s+0\s+R", obj_blob)
        if not refs:
            am = re.search(rb"/Contents\s*\[(.*?)\]", obj_blob, re.S)
            if am:
                refs = re.findall(rb"(\d+)\s+0\s+R", am.group(1))
        parts: list[bytes] = []
        for r in refs:
            num = int(r)
            if num in spans:
                _raw, dec = _stream_payload(spans[num])
                parts.append(dec or _raw or b"")
        if parts:
            return b"\n".join(parts)
        return b""

    def walk(obj_blob: bytes, inherited_fonts: dict[str, int]) -> None:
        res = resources_for(obj_blob)
        fonts = dict(inherited_fonts)
        fonts.update(_font_dict_from_resources(res, spans))
        content = content_of(obj_blob)
        if not content and b"stream" in obj_blob[:80]:
            # Form XObject itself is a stream
            _raw, dec = _stream_payload(obj_blob)
            content = dec or _raw or b""
        if content:
            scopes.append((content, fonts))
        for _name, num in _xobject_map(res, spans).items():
            if num in seen_forms or num not in spans:
                continue
            form = spans[num]
            if b"/Subtype/Form" not in form and b"/Subtype /Form" not in form:
                # still try if Type/XObject Form
                if b"/Form" not in form:
                    continue
            seen_forms.add(num)
            walk(form, fonts)

    if page_blob:
        walk(page_blob, {})
    else:
        primary = content_stream_bytes(pdf)
        if primary:
            scopes.append((primary, {}))
        else:
            for _raw, dec in find_streams(pdf):
                if dec and is_content_stream(dec):
                    scopes.append((dec, {}))
    return scopes


def extract_used_cids_scoped(pdf: bytes) -> dict[str, set[int]]:
    """CIDs from Tj/TJ/'/\" under Tf, with Form XObject resource scope."""
    used: dict[str, set[int]] = {}
    for content, font_map in _collect_content_scopes(pdf):
        # Map resource aliases: if Tf uses /F1 resolve via font_map presence.
        current = ""
        pos = 0
        while pos < len(content):
            tf = _TF_RE.search(content, pos)
            show = _SHOW_RE.search(content, pos)
            if tf and (not show or tf.start() <= show.start()):
                name = tf.group(1).decode("ascii")
                # Prefer F1/F2/F3 style; accept any mapped font resource name.
                if name in font_map or name.startswith("F"):
                    current = name
                else:
                    current = name
                pos = tf.end()
                continue
            if not show:
                break
            if current:
                operand = show.group(1)
                if operand.startswith(b"["):
                    for tok in _STR_IN_ARRAY_RE.finditer(operand):
                        for cid in _cids_from_operand(tok.group(0)):
                            used.setdefault(current, set()).add(cid)
                else:
                    for cid in _cids_from_operand(operand):
                        used.setdefault(current, set()).add(cid)
            pos = show.end()
    return used


def parse_sfnt_tables(ttf: bytes) -> dict[bytes, bytes] | None:
    if not ttf or len(ttf) < 12:
        return None
    try:
        num_tables = struct.unpack(">H", ttf[4:6])[0]
    except struct.error:
        return None
    need = 12 + num_tables * 16
    if len(ttf) < need or num_tables < 1 or num_tables > 64:
        return None
    tables: dict[bytes, bytes] = {}
    for i in range(num_tables):
        o = 12 + i * 16
        tag, _cs, offset, length = struct.unpack(">4sIII", ttf[o:o + 16])
        if offset + length > len(ttf):
            return None
        tables[tag] = ttf[offset:offset + length]
    return tables


def _loca_offsets(tables: dict[bytes, bytes]) -> tuple[list[int], int, int] | None:
    head = tables.get(b"head")
    maxp = tables.get(b"maxp")
    loca = tables.get(b"loca")
    if not head or not maxp or not loca or len(head) < 54 or len(maxp) < 6:
        return None
    index_to_loc_format = struct.unpack(">H", head[50:52])[0]
    num_glyphs = struct.unpack(">H", maxp[4:6])[0]
    offsets: list[int] = []
    if index_to_loc_format == 0:
        if len(loca) < (num_glyphs + 1) * 2:
            return None
        for i in range(num_glyphs + 1):
            offsets.append(struct.unpack(">H", loca[i * 2:i * 2 + 2])[0] * 2)
    else:
        if len(loca) < (num_glyphs + 1) * 4:
            return None
        for i in range(num_glyphs + 1):
            offsets.append(struct.unpack(">I", loca[i * 4:i * 4 + 4])[0])
    return offsets, num_glyphs, index_to_loc_format


def _hmtx_metrics(
    tables: dict[bytes, bytes], gid: int, number_of_h_metrics: int,
) -> tuple[int | None, int | None]:
    hmtx = tables.get(b"hmtx")
    if not hmtx or number_of_h_metrics < 1:
        return None, None
    if gid < number_of_h_metrics:
        o = gid * 4
        if o + 4 > len(hmtx):
            return None, None
        aw, lsb = struct.unpack(">Hh", hmtx[o:o + 4])
        return int(aw), int(lsb)
    # side bearing only
    last_aw_off = (number_of_h_metrics - 1) * 4
    if last_aw_off + 2 > len(hmtx):
        return None, None
    aw = struct.unpack(">H", hmtx[last_aw_off:last_aw_off + 2])[0]
    lsb_off = number_of_h_metrics * 4 + (gid - number_of_h_metrics) * 2
    if lsb_off + 2 > len(hmtx):
        return int(aw), None
    lsb = struct.unpack(">h", hmtx[lsb_off:lsb_off + 2])[0]
    return int(aw), int(lsb)


def _glyph_slice(tables: dict[bytes, bytes], offsets: list[int], gid: int) -> bytes:
    glyf = tables.get(b"glyf") or b""
    if gid < 0 or gid + 1 >= len(offsets):
        return b""
    a, b = offsets[gid], offsets[gid + 1]
    if b <= a or a >= len(glyf):
        return b""
    return glyf[a:min(b, len(glyf))]


def _parse_composite_components(glyph_data: bytes) -> list[int]:
    """Return component GIDs following MORE_COMPONENTS chain."""
    if len(glyph_data) < 10:
        return []
    ncont = struct.unpack(">h", glyph_data[0:2])[0]
    if ncont >= 0:
        return []
    pos = 10  # skip contours + bbox
    comps: list[int] = []
    while pos + 4 <= len(glyph_data):
        flags, gindex = struct.unpack(">HH", glyph_data[pos:pos + 4])
        pos += 4
        comps.append(gindex)
        if flags & _ARG_1_AND_2_ARE_WORDS:
            pos += 4
        else:
            pos += 2
        if flags & _WE_HAVE_A_SCALE:
            pos += 2
        elif flags & _WE_HAVE_AN_X_AND_Y_SCALE:
            pos += 4
        elif flags & _WE_HAVE_A_TWO_BY_TWO:
            pos += 8
        if not (flags & _MORE_COMPONENTS):
            break
    return comps


def recursive_composite_closure(
    tables: dict[bytes, bytes],
    offsets: list[int],
    seed: set[int],
) -> set[int]:
    required = set(seed)
    stack = list(seed)
    while stack:
        gid = stack.pop()
        data = _glyph_slice(tables, offsets, gid)
        if len(data) < 2:
            continue
        ncont = struct.unpack(">h", data[0:2])[0]
        if ncont >= 0:
            continue
        for cgid in _parse_composite_components(data):
            if cgid not in required:
                required.add(cgid)
                stack.append(cgid)
    return required


def _matches_f1_static_profile(tables: dict[bytes, bytes]) -> tuple[bool, dict[str, Any]]:
    head = tables.get(b"head")
    maxp = tables.get(b"maxp")
    hhea = tables.get(b"hhea")
    info: dict[str, Any] = {}
    if not head or not maxp or not hhea or len(head) < 54 or len(maxp) < 6 or len(hhea) < 36:
        return False, info
    upem = struct.unpack(">H", head[18:20])[0]
    num_glyphs = struct.unpack(">H", maxp[4:6])[0]
    nhm = struct.unpack(">H", hhea[34:36])[0]
    info.update({
        "unitsPerEm": upem,
        "numGlyphs": num_glyphs,
        "numberOfHMetrics": nhm,
        "table_lengths": {k.decode("latin1"): len(tables.get(k, b"")) for k in _F1_STATIC_TABLES},
    })
    if upem != _F1_UPEM or num_glyphs != _F1_NUM_GLYPHS or nhm != _F1_NUM_H_METRICS:
        return False, info
    for tag, expected in _F1_STATIC_TABLES.items():
        if len(tables.get(tag, b"")) != expected:
            return False, info
    return True, info


def _f1_type0_ok(pdf: bytes, type0_blob: bytes, desc_blob: bytes) -> tuple[bool, str]:
    bm = re.search(rb"/BaseFont/([^\s/>]+)", type0_blob)
    base = (bm.group(1).decode("latin1", "replace") if bm else "")
    if not base.endswith("TinkoffSans-Regular") and "+TinkoffSans-Regular" not in base:
        return False, base
    if b"/Subtype/Type0" not in type0_blob and b"/Subtype /Type0" not in type0_blob:
        return False, base
    if b"/Encoding/Identity-H" not in type0_blob and b"/Encoding /Identity-H" not in type0_blob:
        return False, base
    # CIDToGIDMap Identity on descendant CIDFont
    blob = desc_blob or type0_blob
    if b"/CIDToGIDMap/Identity" not in blob and b"/CIDToGIDMap /Identity" not in blob:
        return False, base
    return True, base


def _atlas_unicode_guess(gid: int) -> str:
    """Best-effort diagnostic only — never used for verdict."""
    try:
        from .tbank_glyph_atlas import load_atlas
        atlas = load_atlas()
        f1 = (atlas or {}).get("F1") or (atlas or {}).get("fonts", {}).get("F1") or {}
        # common shapes: {cid: {unicode: ...}} or reverse maps
        if isinstance(f1, dict):
            entry = f1.get(str(gid)) or f1.get(gid)
            if isinstance(entry, dict):
                return str(entry.get("unicode") or entry.get("char") or "")
            # scan values
            for _k, v in f1.items():
                if isinstance(v, dict) and int(v.get("gid", -1) or -1) == gid:
                    return str(v.get("unicode") or v.get("char") or "")
    except Exception:
        pass
    return ""


def check_tbank_f1_orphan_glyph(
    pdf_bytes: bytes,
    *,
    text: str = "",
    producer: str = "",
    creator: str = "",
) -> OrphanGlyphResult:
    out = OrphanGlyphResult()
    gate_ok, gate_stats = profile_gate_f1_orphan(
        pdf_bytes, text=text, producer=producer, creator=creator,
    )
    out.stats.update(gate_stats)
    if not gate_ok:
        out.stats["skipped"] = "profile_gate"
        return out

    graphs = resolve_font_graph(pdf_bytes)
    g1 = graphs.get("F1")
    if not g1 or not g1.fontfile2_decoded:
        out.stats["skipped"] = "no_f1_fontfile2"
        return out

    spans = _obj_spans(pdf_bytes)
    type0 = spans.get(g1.type0_num, b"")
    desc = spans.get(g1.descendant_num, b"") if g1.descendant_num else b""
    type_ok, base_font = _f1_type0_ok(pdf_bytes, type0, desc)
    out.stats["base_font"] = base_font
    if not type_ok:
        out.stats["skipped"] = "f1_type0_profile"
        return out

    tables = parse_sfnt_tables(g1.fontfile2_decoded)
    if not tables:
        out.stats["skipped"] = "sfnt_parse"
        return out
    static_ok, static_info = _matches_f1_static_profile(tables)
    out.stats["f1_static_profile"] = static_info
    if not static_ok:
        out.stats["skipped"] = "f1_static_table_fingerprint"
        return out

    loca_parsed = _loca_offsets(tables)
    if not loca_parsed:
        out.stats["skipped"] = "loca_parse"
        return out
    offsets, num_glyphs, index_to_loc_format = loca_parsed
    out.stats["index_to_loc_format"] = index_to_loc_format

    used_all = extract_used_cids_scoped(pdf_bytes)
    used_cids = set(used_all.get("F1", set()))
    cmap_cids = set(g1.tounicode or {})
    if not cmap_cids and g1.tounicode_decoded:
        cmap_cids = set(_parse_tounicode(g1.tounicode_decoded)[0])
    width_cids = set(g1.widths or {})
    if not width_cids:
        width_cids = set(_parse_w_array(desc or type0))

    out.stats["used_cid_count"] = len(used_cids)
    out.stats["cmap_cid_count"] = len(cmap_cids)
    out.stats["width_cid_count"] = len(width_cids)
    bijection_ok = used_cids == cmap_cids == width_cids
    out.stats["used_cmap_w_bijection"] = bijection_ok
    if not bijection_ok:
        # Existing HARD codes (CID closure / bijection) own this case.
        out.stats["skipped"] = "bijection_failed_defer_to_existing_hard"
        out.stats["used_only"] = sorted(used_cids - cmap_cids)[:20]
        out.stats["cmap_only"] = sorted(cmap_cids - used_cids)[:20]
        out.stats["width_only"] = sorted(width_cids - cmap_cids)[:20]
        return out

    direct_gids = set(cmap_cids)
    required_gids = recursive_composite_closure(tables, offsets, direct_gids)
    nonempty_gids = {
        gid for gid in range(num_glyphs)
        if gid + 1 < len(offsets) and offsets[gid + 1] > offsets[gid]
    }
    orphan_gids = nonempty_gids - required_gids - {0}

    nhm = struct.unpack(">H", tables[b"hhea"][34:36])[0]
    orphan_simple: list[int] = []
    telemetry: list[dict[str, Any]] = []
    for gid in sorted(orphan_gids):
        data = _glyph_slice(tables, offsets, gid)
        if len(data) < 10:
            continue
        ncont = struct.unpack(">h", data[0:2])[0]
        x_min, y_min, x_max, y_max = struct.unpack(">hhhh", data[2:10])
        is_composite = ncont < 0
        aw, lsb = _hmtx_metrics(tables, gid, nhm)
        start, end = offsets[gid], offsets[gid + 1]
        entry = {
            "gid": gid,
            "loca_start": start,
            "loca_end": end,
            "glyph_length": end - start,
            "numberOfContours": ncont,
            "composite": is_composite,
            "advanceWidth": aw,
            "leftSideBearing": lsb,
            "glyph_bbox": [x_min, y_min, x_max, y_max],
            "glyph_payload_sha256": hashlib.sha256(data).hexdigest(),
            "atlas_unicode_guess": _atlas_unicode_guess(gid),
        }
        telemetry.append(entry)
        if ncont > 0:
            orphan_simple.append(gid)

    out.orphan_telemetry = telemetry
    out.stats["orphan_telemetry"] = telemetry
    out.stats["direct_gid_count"] = len(direct_gids)
    out.stats["required_closure_count"] = len(required_gids)
    out.stats["nonempty_gid_count"] = len(nonempty_gids)
    out.stats["orphan_simple_gids"] = orphan_simple
    out.stats["orphan_simple_count"] = len(orphan_simple)

    if orphan_simple:
        out.flags.append(HardFlag(
            code=CODE,
            detail=(
                f"F1 orphan simple glyph(s) outside ToUnicode/W/composite closure: "
                f"orphan_gids={orphan_simple[:24]}"
                + (f" (+{len(orphan_simple) - 24})" if len(orphan_simple) > 24 else "")
                + f" used_gid_count={len(direct_gids)}"
                + f" required_closure_count={len(required_gids)}"
                + f" nonempty_gid_count={len(nonempty_gids)}"
                + f" base_font={base_font}"
            ),
            rule_id=RULE_ID,
        ))
    return out
