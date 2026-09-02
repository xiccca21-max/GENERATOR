"""Oracle BI Publisher subset emitter for Alfa SBP / card.

Alfa / the checker: Subset(source Tahoma, first-paint text). Source is the
reconstructed Oracle Tahoma (shared GIDs, not a pre-cut receipt). CID 1..N
= first paint; composites spawn after mapped glyphs. Copy loca slices and
rewrite glyphIndex only. Never covering-orig copy, never fontTools-recompile,
never unlocked / Windows Tahoma.
"""
from __future__ import annotations

import hashlib
import logging
import pickle
import re
import struct
from typing import Dict, Iterable, List, Optional, Set, Tuple

from alfa_orig_mode import (
    AlfaOrigContext,
    _TM_RE,
)
from alfa_oracle_master import (
    ensure_master,
    missing_chars,
    template_font,
)

logger = logging.getLogger(__name__)

_NBSP = "\u00a0"
NBSP_CID = 10
_COMPETITOR_SFNT = [
    "head", "hhea", "maxp", "hmtx", "fpgm", "prep", "cvt ", "loca", "glyf",
]
_ORACLE_SFNT = [
    "cvt ", "fpgm", "glyf", "head", "hhea", "hmtx", "loca", "maxp", "prep",
]
_ORACLE_PIN_CSA = 1757709444


def sfnt_aligned_end(ttf: bytes) -> int:
    """Oracle SFNT end: 4-pad every table except glyf (glyf may be 2-mod-4).

    align4(max(offset+length)) is wrong when glyf is unpadded — later offsets
    are 2-mod-4, so prep's 4-pad is 2 bytes past that formula. Entropy after
    this natural end is still forbidden.
    """
    import struct

    if len(ttf) < 12:
        return -1
    num_tables = struct.unpack(">H", ttf[4:6])[0]
    directory_end = 12 + num_tables * 16
    if num_tables <= 0 or directory_end > len(ttf):
        return -1
    last_end = directory_end
    for i in range(num_tables):
        rec = 12 + i * 16
        tag = ttf[rec: rec + 4]
        offset, length = struct.unpack(">II", ttf[rec + 8 : rec + 16])
        if offset < directory_end or offset + length > len(ttf):
            return -1
        pad = 0 if tag == b"glyf" else ((4 - (length % 4)) % 4)
        last_end = max(last_end, offset + length + pad)
    return last_end


def sfnt_has_exact_aligned_end(ttf: bytes) -> bool:
    """No private bytes may follow the aligned end of the final SFNT table."""
    return sfnt_aligned_end(ttf) == len(ttf)


def trim_sfnt_to_aligned_end(ttf: bytes) -> bytes:
    """Remove serializer residue outside all SFNT tables; never add padding."""
    end = sfnt_aligned_end(ttf)
    if end < 0 or end > len(ttf):
        raise ValueError("invalid SFNT table directory")
    return ttf[:end]


_ARG_WORDS = 0x0001
_MORE_COMPONENTS = 0x0020
_WE_HAVE_A_SCALE = 0x0008
_WE_HAVE_XY_SCALE = 0x0040
_WE_HAVE_TWO_BY_TWO = 0x0080
_WE_HAVE_INSTRUCTIONS = 0x0100


def _sfnt_tables(ttf: bytes) -> Dict[str, bytes]:
    if len(ttf) < 12:
        return {}
    n = struct.unpack(">H", ttf[4:6])[0]
    out: Dict[str, bytes] = {}
    for i in range(n):
        rec = 12 + i * 16
        tag = ttf[rec: rec + 4].decode("latin-1")
        off, length = struct.unpack(">II", ttf[rec + 8: rec + 16])
        out[tag] = ttf[off: off + length]
    return out


def _oracle_sfnt_from_tables(tables: Dict[str, bytes]) -> bytes:
    """Directory + payloads in Oracle order; glyf unpadded; CSA pin."""
    import math

    from alfa_font_extend import _finalize_sfnt_checksums, _ot_table_checksum

    order = [t for t in _ORACLE_SFNT if t in tables]
    for tag in tables:
        if tag not in order:
            order.append(tag)
    n = len(order)
    entry_selector = int(math.log2(n)) if n else 0
    search_range = (2 ** entry_selector) * 16
    range_shift = n * 16 - search_range
    header = b"\x00\x01\x00\x00" + struct.pack(
        ">HHHH", n, search_range, entry_selector, range_shift,
    )
    data_start = 12 + n * 16
    current = data_start
    body = b""
    recs = []
    for tag in order:
        payload = tables[tag]
        pad = b"" if tag == "glyf" else (b"\x00" * ((4 - (len(payload) % 4)) % 4))
        recs.append((tag, current, len(payload), _ot_table_checksum(payload)))
        body += payload + pad
        current += len(payload) + len(pad)
    directory = b""
    for tag, off, length, checksum in recs:
        directory += tag.encode("latin-1") + struct.pack(">III", checksum, off, length)
    return trim_sfnt_to_aligned_end(_finalize_sfnt_checksums(header + directory + body))


def _composite_component_gids(raw: bytes) -> List[int]:
    if len(raw) < 10:
        return []
    if struct.unpack(">h", raw[:2])[0] >= 0:
        return []
    pos = 10
    out: List[int] = []
    while pos + 4 <= len(raw):
        flags, gid = struct.unpack(">HH", raw[pos: pos + 4])
        out.append(gid)
        pos += 4
        pos += 4 if flags & _ARG_WORDS else 2
        if flags & _WE_HAVE_A_SCALE:
            pos += 2
        elif flags & _WE_HAVE_XY_SCALE:
            pos += 4
        elif flags & _WE_HAVE_TWO_BY_TWO:
            pos += 8
        if not (flags & _MORE_COMPONENTS):
            break
    return out


def _patch_composite_gids(raw: bytes, new_gids: List[int]) -> bytes:
    """Keep Oracle composite flags/args; only rewrite component glyphIndex."""
    if len(raw) < 10 or struct.unpack(">h", raw[:2])[0] >= 0 or not new_gids:
        return raw
    buf = bytearray(raw)
    pos = 10
    i = 0
    while pos + 4 <= len(buf) and i < len(new_gids):
        flags = struct.unpack(">H", buf[pos: pos + 2])[0]
        buf[pos + 2: pos + 4] = struct.pack(">H", int(new_gids[i]) & 0xFFFF)
        pos += 4
        i += 1
        pos += 4 if flags & _ARG_WORDS else 2
        if flags & _WE_HAVE_A_SCALE:
            pos += 2
        elif flags & _WE_HAVE_XY_SCALE:
            pos += 4
        elif flags & _WE_HAVE_TWO_BY_TWO:
            pos += 8
        if not (flags & _MORE_COMPONENTS):
            break
    return bytes(buf)


def _even_glyf_record(raw: bytes) -> bytes:
    if raw and len(raw) % 2:
        return raw + b"\x00"
    return raw


def _assemble_glyf_loca(
    records: List[bytes],
    occ_of: Dict[int, List[int]],
) -> Tuple[bytes, bytes]:
    blob = b""
    locs = [0]
    for gid, raw in enumerate(records):
        kids = occ_of.get(gid) or []
        if kids:
            raw = _patch_composite_gids(raw, kids)
        raw = _even_glyf_record(raw or b"")
        blob += raw
        locs.append(len(blob))
    loca = struct.pack(">" + "I" * len(locs), *locs)
    return blob, loca


def _hmtx_payload(metrics: List[Tuple[int, int]]) -> bytes:
    return b"".join(struct.pack(">Hh", aw & 0xFFFF, lsb) for aw, lsb in metrics)


def _inject_subset_payload(
    template_ff2: bytes,
    glyf: bytes,
    loca: bytes,
    hmtx: bytes,
    n_glyphs: int,
) -> bytes:
    tables = _sfnt_tables(template_ff2)
    if not tables:
        raise RuntimeError("glyph mismatch template-empty")
    maxp = bytearray(tables.get("maxp") or b"\x00" * 32)
    if len(maxp) < 6:
        maxp = bytearray(maxp) + b"\x00" * (6 - len(maxp))
    struct.pack_into(">H", maxp, 4, int(n_glyphs) & 0xFFFF)
    hhea = bytearray(tables.get("hhea") or b"\x00" * 36)
    if len(hhea) >= 36:
        struct.pack_into(">H", hhea, 34, int(n_glyphs) & 0xFFFF)
    tables["glyf"] = glyf
    tables["loca"] = loca
    tables["hmtx"] = hmtx
    tables["maxp"] = bytes(maxp)
    tables["hhea"] = bytes(hhea)
    return _oracle_sfnt_from_tables(tables)


# Proton ALFA_ORACLE_SBP_HMTX_UNIQ_ADVANCES: Oracle SBP Tahoma n=14 is 42–45
# unique positive hmtx advances when FontFile2 ≥ 20570. Card stays 30–32.
_ORACLE_SBP_HMTX_UNIQ_FLOOR = 42
_ORACLE_SBP_HMTX_UNIQ_MIN_FF2 = 20570


def _hmtx_uniq_positive(metrics: Iterable[Tuple[int, int]]) -> int:
    return len({int(aw) for aw, _lsb in metrics if int(aw) > 0})


def _ensure_hmtx_uniq_floor(
    metrics: List[Tuple[int, int]],
    painted: Set[int],
    floor: int,
    parent_vocab: Optional[Iterable[int]] = None,
    upem: int = 2048,
) -> List[Tuple[int, int]]:
    """Split duplicate unused advances until unique positive AWs ≥ floor.

    Painted CIDs keep their source Tahoma advance (layout). New AWs prefer
    widths already present on parent Tahoma so the vocabulary is restored,
    not invented. Same `_pdf_w` first so /W tokens stay stable.
    """
    if floor <= 0:
        return metrics
    pos = {int(aw) for aw, _lsb in metrics if int(aw) > 0}
    if len(pos) >= floor:
        return metrics
    from collections import Counter

    counts = Counter(int(aw) for aw, _lsb in metrics if int(aw) > 0)
    out = [(int(aw), int(lsb)) for aw, lsb in metrics]
    unused = [
        i
        for i in range(len(out))
        if i not in painted and int(out[i][0]) > 0 and counts[int(out[i][0])] > 1
    ]
    taken = set(pos)
    parent_pool: List[int] = []
    seen_parent: Set[int] = set()
    for aw in parent_vocab or ():
        v = int(aw)
        if v <= 0 or v in taken or v in seen_parent:
            continue
        seen_parent.add(v)
        parent_pool.append(v)

    def _pick(aw: int) -> Optional[int]:
        want_w = _pdf_w(aw, upem)
        for v in parent_pool:
            if v not in taken and _pdf_w(v, upem) == want_w:
                return v
        for delta in range(1, 80):
            for v in (aw + delta, aw - delta):
                if v > 0 and v not in taken and _pdf_w(v, upem) == want_w:
                    return v
        for v in parent_pool:
            if v not in taken:
                return v
        for delta in range(1, 500):
            for v in (aw + delta, max(1, aw - delta)):
                if v not in taken:
                    return v
        return None

    for i in unused:
        if len(taken) >= floor:
            break
        aw, lsb = out[i]
        cand = _pick(aw)
        if cand is None:
            continue
        counts[aw] -= 1
        counts[cand] += 1
        taken.add(cand)
        out[i] = (cand, lsb)
        parent_pool = [v for v in parent_pool if v != cand]
    if len(taken) < floor:
        logger.warning(
            "Alfa SBP hmtx uniq still %d < %d (unused-dup=%d)",
            len(taken), floor, len(unused),
        )
    else:
        logger.info("Alfa SBP hmtx uniq → %d (floor %d)", len(taken), floor)
    return out


def _pack_oracle_raw(
    rec_of_gid: Dict[int, dict],
    occ_of: Dict[int, List[int]],
    uni_to_cid: Dict[int, int],
    cid_to_uni: Dict[int, int],
    template_ff2: bytes,
    *,
    hmtx_uniq_floor: int = 0,
    painted_gids: Optional[Set[int]] = None,
    parent_vocab: Optional[Iterable[int]] = None,
) -> Tuple[bytes, Dict[int, int], Dict[int, int], Dict[int, int]]:
    n_total = max(rec_of_gid) + 1
    records: List[bytes] = []
    metrics: List[Tuple[int, int]] = []
    for gid in range(n_total):
        rec = rec_of_gid[gid]
        raw = rec.get("glyf_raw") or b""
        records.append(raw)
        metrics.append((int(rec["aw"]), int(rec["lsb"])))
    if hmtx_uniq_floor > 0:
        painted = set(painted_gids or ())
        metrics = _ensure_hmtx_uniq_floor(
            metrics, painted, hmtx_uniq_floor, parent_vocab=parent_vocab,
        )
        for gid, (aw, lsb) in enumerate(metrics):
            rec_of_gid[gid]["aw"] = aw
            rec_of_gid[gid]["lsb"] = lsb
    glyf, loca = _assemble_glyf_loca(records, occ_of)
    ttf = _inject_subset_payload(
        template_ff2, glyf, loca, _hmtx_payload(metrics), n_total,
    )
    cid_w = {gid: _pdf_w(rec_of_gid[gid]["aw"]) for gid in range(n_total)}
    return ttf, uni_to_cid, cid_to_uni, cid_w


def _pdf_w(aw: int, upem: int = 2048) -> int:
    """Oracle BI: truncate hmtx → 1000-em, never round-to-nearest."""
    u = upem if upem > 0 else 2048
    return max(1, int(int(aw) * 1000 / u))


def _pdf_w_quartz(aw: int, upem: int = 2048) -> int:
    """iOS Quartz phone donors round hmtx → 1000-em (601 not 600)."""
    u = upem if upem > 0 else 2048
    return max(1, int(round(int(aw) * 1000 / u)))


def _serialize_w_oracle(widths: Dict[int, int]) -> bytes:
    """One `CID [w]` per glyph, space-separated, CRLF after every 10th record.

    Matches Oracle BI: `/W [ 0 [473] 1 [600] ... 9 [562]\\r\\n 10 [312] ... ]`
    """
    if not widths:
        return b"/W [ ]"
    cids = sorted(widths)
    chunks: List[str] = []
    for i, cid in enumerate(cids):
        item = f"{cid} [{int(widths[cid])}]"
        if i == 0:
            chunks.append(item)
        elif i % 10 == 0:
            chunks.append("\r\n " + item)
        else:
            chunks.append(" " + item)
    return ("/W [ " + "".join(chunks) + " ]").encode("ascii")


def _serialize_w_quartz(widths: Dict[int, int]) -> bytes:
    """Indirect Quartz array body: ``[ 1 [ w1 w2 … ] ]`` (no CID0, no ``/W``)."""
    cids = [c for c in sorted(widths) if c >= 1]
    if not cids:
        return b"[ 1 [ ] ]"
    # Expect a contiguous run starting at 1 (FO 1..N).
    ws = [str(int(widths[c])) for c in cids]
    # Soft wrap like donor (~17 widths per line).
    lines: List[str] = []
    for i in range(0, len(ws), 17):
        lines.append(" ".join(ws[i : i + 17]))
    body = "\n".join(lines)
    return f"[ 1 [ {body} ] ]".encode("ascii")


def _rebuild_tounicode_quartz(old_tu: bytes, cid_to_uni: Dict[int, int]) -> bytes:
    """Keep Adobe/Quartz CMap shell; replace bfrange with one-CID rows."""
    text = old_tu.decode("latin1", "replace")
    nl = "\n" if "\r\n" not in text else "\r\n"
    pairs = []
    # CID 0 → ? keeps FO/cmap closed like Oracle emit (donor skipped it).
    pairs.append((0, int(cid_to_uni.get(0) or 0x003F)))
    for cid, uni in sorted(cid_to_uni.items()):
        if cid == 0:
            continue
        if uni and uni != 0xFFFF:
            pairs.append((cid, uni))
    body = "".join(f"<{cid:04X}><{cid:04X}><{uni:04X}>{nl}" for cid, uni in pairs)
    block = f"{len(pairs)} beginbfrange{nl}{body}endbfrange"
    new_text, n = re.subn(
        r"\d+\s+beginbfrange.*?endbfrange(?:\s*\d+\s+beginbfrange.*?endbfrange)*",
        block,
        text,
        count=1,
        flags=re.S | re.I,
    )
    if n:
        # Drop leftover bfchar if any.
        new_text = re.sub(r"\d+\s+beginbfchar.*?endbfchar\s*", "", new_text, flags=re.S | re.I)
        return new_text.encode("latin1")
    # Fallback: wrap Adobe Identity like the donor header.
    return (
        f"/CIDInit /ProcSet findresource begin{nl}"
        f"12 dict begin{nl}begincmap{nl}"
        f"/CIDSystemInfo <<{nl}"
        f"  /Registry (Adobe){nl}"
        f"  /Ordering (UCS){nl}"
        f"  /Supplement 0{nl}"
        f">> def{nl}"
        f"/CMapName /Adobe-Identity-UCS def{nl}"
        f"/CMapType 2 def{nl}"
        f"1 begincodespacerange{nl}<0000><FFFF>{nl}endcodespacerange{nl}"
        f"{block}{nl}"
        f"endcmap{nl}CMapName currentdict /CMap defineresource pop{nl}end{nl}end{nl}"
    ).encode("latin1")


def _raw_w_array(pdf: bytes, cid_xref: int) -> bytes:
    from tbank_orig_mode import find_object_range

    rng = find_object_range(pdf, cid_xref)
    if not rng:
        return b""
    obj = pdf[rng[0] : rng[1]]
    m = re.search(rb"/W\s*\[", obj)
    if not m:
        return b""
    i = m.end() - 1
    depth = 0
    while i < len(obj):
        if obj[i : i + 1] == b"[":
            depth += 1
        elif obj[i : i + 1] == b"]":
            depth -= 1
            if depth == 0:
                return obj[m.start() : i + 1]
        i += 1
    return b""


def _name_for_gid(gid: int) -> str:
    return ".notdef" if gid == 0 else f"g{gid:05d}"


_ORACLE_FONTBBOX = (-599, -207, 1338, 1034)
_ORACLE_FONTBBOX_PDF = "/FontBBox [ -599 -207 1338 1034 ]"
# Mid-map U+0000 residues immediately after ь. Live pass_middle_nul pair:
# й (composite) + Ч, full Tahoma outline/hints/metrics — never width-only.
_SOFT_SIGN_CP = 0x044C
_RESIDUE_SPECS: Tuple[Tuple[int, str, int, int], ...] = (
    (
        0x0439,  # й
        "42cb1e308609db9a4010ab0f408e6a4120ae4ae04cbfeb7aff34a82b60270c7f",
        1154,
        136,
    ),
    (
        0x0427,  # Ч
        "76afad397b5929d8e9f42fa38d48bcd03cc2ead780a742f071510bbe5bad7b2c",
        1302,
        93,
    ),
)
_FORBIDDEN_RESIDUE: frozenset = frozenset({
    ("97d4546c1da3f62ac2b24012be9356d200e146f1e4cc528aefcf2549b69ef582", 965, 42),  # э
    ("33a4d608c980e7a7d5866cf674911164b9d3a9e9177c4ed23f8ac8093c851d49", 1164, 4),  # ъ
})


def _copy_residue_record(cp: int) -> dict:
    from alfa_oracle_master import load_glyph

    rec = load_glyph(cp)
    if rec is None:
        raise RuntimeError(f"glyph mismatch residue-src:U+{cp:04X}")
    item = dict(rec)
    item["cp"] = 0
    return item


def _pick_middle_orphans(seen: set, n: int = 2, seed: bytes = b"") -> List[dict]:
    """Always й then Ч after ь. `seen`/`seed` do not pick substitutes."""
    del seen, n, seed
    return [_copy_residue_record(cp) for cp, _digest, _aw, _lsb in _RESIDUE_SPECS]


def _residue_tuple(tt, cid: int) -> Tuple[str, int, int]:
    from alfa_oracle_master import _glyph_outline_sha

    order = tt.getGlyphOrder()
    name = order[cid]
    aw, lsb = tt["hmtx"].metrics[name]
    digest = _glyph_outline_sha(tt, cid)
    return digest, int(aw), int(lsb)


def _validate_residue_pair(ttf: bytes, cid_to_uni: Dict[int, int]) -> str:
    """Exact (outline_hash, advanceWidth, lsb) after ь; else generation error."""
    from fontTools.ttLib import TTFont
    from io import BytesIO

    soft = [c for c, u in cid_to_uni.items() if u == _SOFT_SIGN_CP]
    nuls = [c for c, u in sorted(cid_to_uni.items()) if c and u == 0]
    if len(soft) != 1:
        return f"glyph mismatch residue-soft:{soft}"
    want_cids = [soft[0] + 1, soft[0] + 2]
    if nuls != want_cids:
        return f"glyph mismatch residue-cids:{nuls}!={want_cids}"
    tt = TTFont(BytesIO(ttf))
    try:
        order = tt.getGlyphOrder()
        if max(nuls) >= len(order):
            return "glyph mismatch residue-gid"
        got = [_residue_tuple(tt, cid) for cid in nuls]
    finally:
        tt.close()
    for tup in got:
        if (tup[0], tup[1], tup[2]) in _FORBIDDEN_RESIDUE:
            return f"glyph mismatch residue-forbidden:{tup[0][:16]}/{tup[1]}/{tup[2]}"
        if tup[0][:16] in ("97d4546c1da3", "33a4d608c980"):
            return f"glyph mismatch residue-forbidden:{tup[0][:16]}"
    want = [(digest, aw, lsb) for _cp, digest, aw, lsb in _RESIDUE_SPECS]
    if got != want:
        detail = ",".join(f"{h[:16]}/{aw}/{lsb}" for h, aw, lsb in got)
        return f"glyph mismatch residue-tuple:{detail}"
    return ""


def build_subset(
    ordered_cps: Iterable[int],
    seed: bytes = b"",
    *,
    profile: str = "repack",
) -> Tuple[bytes, Dict[int, int], Dict[int, int], Dict[int, int]]:
    """PASS-shaped subset (pass_middle_nul residues).

    .notdef + painted CIDs. Immediately after ь: two ToUnicode-U+0000
    copies of й and Ч (full outline/hints/metrics). Composite components
    follow mapped CIDs. SFNT order is competitor (head…glyf) with textbook
    OpenType CSA.
    """
    from alfa_font_extend import _save_oracle_ttf
    from alfa_oracle_master import ensure_master, load_extra, load_glyph

    bank = ensure_master()
    notdef = dict(bank["notdef"])
    printable: List[int] = []
    seen: set = set()
    for cp in ordered_cps:
        if cp == 0x20:
            cp = 0x00A0
        if cp < 32 and cp != 0x00A0:
            continue
        if cp in seen:
            continue
        if load_glyph(cp) is None:
            if profile != "repack":
                raise RuntimeError(f"glyph mismatch missing-U+{cp:04X}")
            continue
        seen.add(cp)
        printable.append(cp)
    if 0x00A0 not in seen:
        printable.append(0x00A0)

    rec_of_gid: Dict[int, dict] = {0: notdef}
    uni_to_cid: Dict[int, int] = {}
    cid_to_uni: Dict[int, int] = {0: 0x003F}
    next_cid = 1
    orphans = _pick_middle_orphans(seen, 2, seed) if profile == "repack" else []
    if profile == "repack":
        if _SOFT_SIGN_CP not in seen:
            raise RuntimeError("glyph mismatch residue-no-soft")
        if len(orphans) != 2:
            raise RuntimeError("glyph mismatch residue-count")
    items: List[Tuple[str, object]] = []
    placed = False
    for cp in printable:
        items.append(("map", cp))
        if cp == _SOFT_SIGN_CP and orphans:
            for rec in orphans:
                items.append(("nul", rec))
            placed = True
    if orphans and not placed:
        raise RuntimeError("glyph mismatch residue-not-after-soft")
    for kind, payload in items:
        if kind == "map":
            cp = int(payload)
            rec = load_glyph(cp)
            if rec is None:
                if profile != "repack":
                    raise RuntimeError(f"glyph mismatch missing-U+{cp:04X}")
                continue
            rec = dict(rec)
            rec["cp"] = cp
            rec_of_gid[next_cid] = rec
            uni_to_cid[cp] = next_cid
            cid_to_uni[next_cid] = cp
            next_cid += 1
        else:
            rec_of_gid[next_cid] = dict(payload)
            cid_to_uni[next_cid] = 0
            next_cid += 1
    n_mapped = max(rec_of_gid)

    occ_of: Dict[int, List[int]] = {}

    def _record_for_ident(ident_key: str) -> dict:
        extra = load_extra(ident_key)
        if extra is not None:
            return dict(extra)
        for rec in rec_of_gid.values():
            if rec.get("ident_key") == ident_key:
                return dict(rec)
        return dict(notdef)

    def spawn(ident_key: str) -> int:
        extra = _record_for_ident(ident_key)
        gid = max(rec_of_gid) + 1
        rec_of_gid[gid] = extra
        keys = extra.get("components") or []
        if keys:
            occ_of[gid] = [spawn(ck) for ck in keys]
        return gid

    for gid in range(1, n_mapped + 1):
        rec = rec_of_gid[gid]
        keys = rec.get("components") or []
        if keys:
            occ_of[gid] = [spawn(ck) for ck in keys]

    n_total = max(rec_of_gid) + 1
    if profile != "repack":
        template_ff2 = bank.get("template_ff2") or b""
        if not template_ff2:
            raise RuntimeError("glyph mismatch no-template")
        return _pack_oracle_raw(
            rec_of_gid, occ_of, uni_to_cid, cid_to_uni, template_ff2,
        )
    order = [_name_for_gid(i) for i in range(n_total)]

    from fontTools.ttLib import newTable
    from fontTools.ttLib.tables._g_l_y_f import table__g_l_y_f

    font = template_font()
    new_glyf = table__g_l_y_f()
    new_glyf.glyphs = {}
    new_glyf.glyphOrder = order
    hmtx = {}
    for gid in range(n_total):
        rec = rec_of_gid[gid]
        g = pickle.loads(rec["glyph_pkl"])
        if int(getattr(g, "numberOfContours", 0) or 0) == -1:
            occ = occ_of.get(gid) or []
            for i, comp in enumerate(getattr(g, "components", []) or []):
                if i < len(occ):
                    comp.glyphName = _name_for_gid(occ[i])
        name = order[gid]
        new_glyf.glyphs[name] = g
        hmtx[name] = (int(rec["aw"]), int(rec["lsb"]))

    font["maxp"].numGlyphs = n_total
    font["glyf"] = new_glyf
    font.setGlyphOrder(order)
    mtx = newTable("hmtx")
    mtx.metrics = hmtx
    font["hmtx"] = mtx
    font["hhea"].numberOfHMetrics = n_total
    for tag in ("cmap", "name", "post", "OS/2"):
        if tag in font:
            del font[tag]

    ttf = _save_oracle_ttf(
        font,
        template_font(),
        oracle_native=(profile != "repack"),
        competitor_repack=(profile == "repack"),
    )
    ttf = trim_sfnt_to_aligned_end(ttf)
    if profile == "repack":
        why = _validate_residue_pair(ttf, cid_to_uni)
        if why:
            raise RuntimeError(why)
    upem = int(font["head"].unitsPerEm or 2048)
    cid_w: Dict[int, int] = {}
    for gid in range(n_total):
        cid_w[gid] = _pdf_w(rec_of_gid[gid]["aw"], upem)
    return ttf, uni_to_cid, cid_to_uni, cid_w


def _glyf_component_gids(tt, gid: int, acc: Set[int]) -> None:
    order = tt.getGlyphOrder()
    if gid < 0 or gid >= len(order):
        return
    g = tt["glyf"][order[gid]]
    if int(getattr(g, "numberOfContours", 0) or 0) != -1:
        return
    for comp in getattr(g, "components", []) or []:
        name = getattr(comp, "glyphName", None)
        if not name or name not in order:
            continue
        cgid = order.index(name)
        if cgid in acc:
            continue
        acc.add(cgid)
        _glyf_component_gids(tt, cgid, acc)


def build_tight_from_ttf(
    ttf: bytes,
    cid_to_uni: Dict[int, int],
    ordered_cps: Iterable[int],
) -> Tuple[bytes, Dict[int, int], Dict[int, int], Dict[int, int]]:
    """Subset an existing FontFile2: .notdef + painted + composite components.

    ToUnicode = CID 0 + painted CIDs only. No U+0000 residue, no orphan GIDs.
    """
    from copy import deepcopy
    from io import BytesIO

    from fontTools.ttLib import TTFont, newTable
    from fontTools.ttLib.tables._g_l_y_f import table__g_l_y_f

    from alfa_font_extend import _save_oracle_ttf

    tt = TTFont(BytesIO(ttf))
    old_order = list(tt.getGlyphOrder())
    old_u2c: Dict[int, int] = {}
    for cid, uni in cid_to_uni.items():
        if cid == 0 or not uni:
            continue
        old_u2c.setdefault(uni, cid)

    painted_old: List[int] = []
    seen: Set[int] = set()
    for cp in ordered_cps:
        if cp == 0x20:
            cp = 0x00A0
        if cp < 32 and cp != 0x00A0:
            continue
        old_cid = old_u2c.get(cp)
        if old_cid is None or old_cid in seen:
            continue
        if old_cid >= len(old_order):
            continue
        seen.add(old_cid)
        painted_old.append(old_cid)
    if not painted_old:
        tt.close()
        raise RuntimeError("tight subset: no painted CIDs")

    # Oracle / BI Publisher: components are copied in first-paint order, not
    # shared and not sorted by source GID. Same letter as a mapped CID still
    # gets a fresh unmapped copy when a composite needs it.
    new_to_old: List[int] = [0]
    new_c2u: Dict[int, int] = {0: cid_to_uni.get(0) or 0x003F}
    if new_c2u[0] == 0:
        new_c2u[0] = 0x003F
    new_u2c: Dict[int, int] = {}
    for old_cid in painted_old:
        new_to_old.append(old_cid)
        uni = cid_to_uni[old_cid]
        new_c2u[len(new_to_old) - 1] = uni
        new_u2c[uni] = len(new_to_old) - 1
    n_mapped = len(new_to_old) - 1
    occ_of: Dict[int, List[int]] = {}

    def _spawn_old(old_gid: int) -> int:
        new_to_old.append(old_gid)
        new_gid = len(new_to_old) - 1
        g = tt["glyf"][old_order[old_gid]]
        if int(getattr(g, "numberOfContours", 0) or 0) == -1:
            kids: List[int] = []
            for comp in getattr(g, "components", []) or []:
                cname = getattr(comp, "glyphName", None)
                if not cname or cname not in old_order:
                    continue
                kids.append(_spawn_old(old_order.index(cname)))
            occ_of[new_gid] = kids
        return new_gid

    for new_gid in range(1, n_mapped + 1):
        old_gid = new_to_old[new_gid]
        g = tt["glyf"][old_order[old_gid]]
        if int(getattr(g, "numberOfContours", 0) or 0) != -1:
            continue
        kids = []
        for comp in getattr(g, "components", []) or []:
            cname = getattr(comp, "glyphName", None)
            if not cname or cname not in old_order:
                continue
            kids.append(_spawn_old(old_order.index(cname)))
        occ_of[new_gid] = kids

    n_total = len(new_to_old)
    new_order = [_name_for_gid(i) for i in range(n_total)]
    new_glyf = table__g_l_y_f()
    new_glyf.glyphs = {}
    new_glyf.glyphOrder = new_order
    hmtx: Dict[str, Tuple[int, int]] = {}
    for new_gid in range(n_total):
        old_gid = new_to_old[new_gid]
        old_name = old_order[old_gid]
        g = deepcopy(tt["glyf"][old_name])
        if int(getattr(g, "numberOfContours", 0) or 0) == -1:
            occ = occ_of.get(new_gid) or []
            for i, comp in enumerate(getattr(g, "components", []) or []):
                if i < len(occ):
                    comp.glyphName = _name_for_gid(occ[i])
        name = new_order[new_gid]
        new_glyf.glyphs[name] = g
        aw, lsb = tt["hmtx"].metrics[old_name]
        hmtx[name] = (int(aw), int(lsb))

    font = TTFont(BytesIO(ttf))
    font["glyf"] = new_glyf
    font.setGlyphOrder(new_order)
    font["maxp"].numGlyphs = n_total
    mtx = newTable("hmtx")
    mtx.metrics = hmtx
    font["hmtx"] = mtx
    font["hhea"].numberOfHMetrics = n_total
    for tag in ("cmap", "name", "post", "OS/2"):
        if tag in font:
            del font[tag]
    upem = int(tt["head"].unitsPerEm or 2048)
    template = TTFont(BytesIO(ttf))
    try:
        out = trim_sfnt_to_aligned_end(
            _save_oracle_ttf(font, template, oracle_native=True)
        )
    finally:
        tt.close()
        template.close()
        font.close()
    cid_w = {gid: _pdf_w(hmtx[new_order[gid]][0], upem) for gid in range(n_total)}
    return out, new_u2c, new_c2u, cid_w


def build_tight_from_raw(
    ttf: bytes,
    cid_to_uni: Dict[int, int],
    ordered_cps: Iterable[int],
    *,
    uni_to_gid: Optional[Dict[int, int]] = None,
    hmtx_uniq_floor: int = 0,
) -> Tuple[bytes, Dict[int, int], Dict[int, int], Dict[int, int]]:
    """Subset source Tahoma (or a genuine orig) by copying loca slices.

    Component glyphIndex is rewritten; flags/args/hints stay Oracle bytes.
    CID 1..N = first unique paint; composites spawn after mapped glyphs.
    """
    tables = _sfnt_tables(ttf)
    glyf_blob = tables.get("glyf") or b""
    loca = tables.get("loca") or b""
    n_off = len(loca) // 4
    if n_off < 2:
        raise RuntimeError("tight subset: no loca")
    offs = list(struct.unpack(">%dI" % n_off, loca[: n_off * 4]))
    n_avail = len(offs) - 1
    hmtx_raw = tables.get("hmtx") or b""

    def slice_gid(gid: int) -> bytes:
        if gid < 0 or gid + 1 >= len(offs):
            return b""
        return bytes(glyf_blob[offs[gid]: offs[gid + 1]])

    def hmtx_of(gid: int) -> Tuple[int, int]:
        base = gid * 4
        if base + 4 <= len(hmtx_raw):
            return struct.unpack(">Hh", hmtx_raw[base: base + 4])
        return (1024, 0)

    old_u2c: Dict[int, int] = {}
    old_c2u: Dict[int, int] = {
        int(cid): int(uni) for cid, uni in (cid_to_uni or {}).items() if uni
    }
    if uni_to_gid:
        for cp, gid in uni_to_gid.items():
            old_u2c[int(cp)] = int(gid)
            old_c2u.setdefault(int(gid), int(cp))
        if 0x00A0 in old_u2c:
            old_u2c[0x20] = old_u2c[0x00A0]
            old_c2u[old_u2c[0x00A0]] = 0x00A0
        elif 0x20 in old_u2c:
            old_u2c[0x00A0] = old_u2c[0x20]
    else:
        for cid, uni in cid_to_uni.items():
            if cid == 0 or not uni:
                continue
            old_u2c.setdefault(int(uni), int(cid))

    painted_old: List[int] = []
    painted_uni: List[int] = []
    for cp in ordered_cps:
        if cp == 0x20:
            cp = 0x00A0
        if cp < 32 and cp != 0x00A0:
            continue
        old_cid = old_u2c.get(cp)
        if old_cid is None:
            raise RuntimeError(f"glyph mismatch missing-U+{cp:04X}")
        if old_cid >= n_avail:
            continue
        # Same outline GID may back multiple Unicodes (uppercase aliased to
        # lowercase in PARENT_CMAP). Emit a painted CID per CP — never skip.
        painted_old.append(old_cid)
        painted_uni.append(cp)
    if not painted_old:
        raise RuntimeError("tight subset: no painted CIDs")

    new_to_old: List[int] = [0]
    new_c2u: Dict[int, int] = {0: old_c2u.get(0) or 0x003F}
    if new_c2u[0] == 0:
        new_c2u[0] = 0x003F
    new_u2c: Dict[int, int] = {}
    for old_cid, uni in zip(painted_old, painted_uni):
        new_to_old.append(old_cid)
        new_c2u[len(new_to_old) - 1] = uni
        new_u2c[uni] = len(new_to_old) - 1
    n_mapped = len(new_to_old) - 1
    occ_of: Dict[int, List[int]] = {}

    def spawn_old(old_gid: int) -> int:
        new_to_old.append(old_gid)
        new_gid = len(new_to_old) - 1
        kids_old = _composite_component_gids(slice_gid(old_gid))
        if kids_old:
            occ_of[new_gid] = [spawn_old(int(k)) for k in kids_old]
        return new_gid

    for new_gid in range(1, n_mapped + 1):
        old_gid = new_to_old[new_gid]
        kids_old = _composite_component_gids(slice_gid(old_gid))
        if kids_old:
            occ_of[new_gid] = [spawn_old(int(k)) for k in kids_old]

    n_total = len(new_to_old)
    rec_of_gid: Dict[int, dict] = {}
    for gid in range(n_total):
        aw, lsb = hmtx_of(new_to_old[gid])
        rec_of_gid[gid] = {
            "glyf_raw": slice_gid(new_to_old[gid]),
            "aw": int(aw),
            "lsb": int(lsb),
        }
    parent_vocab = [int(hmtx_of(gid)[0]) for gid in range(n_avail)]
    painted = set(range(1, n_mapped + 1))
    return _pack_oracle_raw(
        rec_of_gid, occ_of, new_u2c, new_c2u, ttf,
        hmtx_uniq_floor=hmtx_uniq_floor,
        painted_gids=painted,
        parent_vocab=parent_vocab,
    )


def subset_from_parent(
    ordered_cps: Iterable[int],
    *,
    hmtx_uniq_floor: int = 0,
) -> Tuple[bytes, Dict[int, int], Dict[int, int], Dict[int, int]]:
    """Subset(source Oracle Tahoma, first-paint) — same function as the bank."""
    import json
    import os

    from alfa_oracle_master import PARENT_CMAP, PARENT_TTF, materialize_parent

    bank = ensure_master()
    raw: dict = {}
    need = True
    if os.path.isfile(PARENT_TTF) and os.path.isfile(PARENT_CMAP):
        with open(PARENT_CMAP, encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw.get("_format") == "source-shared":
            need = False
    if need:
        materialize_parent(bank)
        with open(PARENT_CMAP, encoding="utf-8") as fh:
            raw = json.load(fh)
    if not os.path.isfile(PARENT_TTF):
        raise RuntimeError("Oracle source Tahoma missing")
    with open(PARENT_TTF, "rb") as fh:
        parent = fh.read()
    uni_to_gid: Dict[int, int] = {
        int(k): int(v) for k, v in raw.items() if str(k).isdigit()
    }
    if not uni_to_gid:
        raise RuntimeError("Oracle source Tahoma cmap empty")
    return build_tight_from_raw(
        parent, {}, ordered_cps, uni_to_gid=uni_to_gid,
        hmtx_uniq_floor=hmtx_uniq_floor,
    )


def subset_from_origs(
    ordered_cps: Iterable[int],
    *,
    hmtx_uniq_floor: int = 0,
) -> Tuple[bytes, Dict[int, int], Dict[int, int], Dict[int, int]]:
    """Alfa emit = Subset(source Tahoma, first-paint). Not a covering-orig copy."""
    return subset_from_parent(ordered_cps, hmtx_uniq_floor=hmtx_uniq_floor)


def enc_text(text: str, uni_to_cid: Dict[int, int]) -> bytes:
    out = []
    for ch in text or "":
        cp = 0x00A0 if ch in (" ", _NBSP) else ord(ch)
        cid = uni_to_cid.get(cp)
        if cid is None:
            return b""
        out.append(f"{cid:04X}")
    return "".join(out).encode("ascii")


def dec_hex(hex_ascii: bytes, cid_to_uni: Dict[int, int]) -> str:
    h = hex_ascii.decode("ascii")
    chars = []
    for i in range(0, len(h), 4):
        cid = int(h[i : i + 4], 16)
        cp = cid_to_uni.get(cid, 0)
        chars.append(chr(cp) if cp else "")
    return "".join(chars)


def collect_stream_unicodes(stream: bytes, cid_to_uni: Dict[int, int]) -> str:
    text = []
    for m in _TM_RE.finditer(stream):
        tail = stream[m.end() : m.end() + 240]
        tj = re.search(rb"<([0-9A-Fa-f]+)>", tail)
        if not tj:
            continue
        text.append(dec_hex(tj.group(1), cid_to_uni))
    return "".join(text)


def collect_label_unicodes(
    stream: bytes,
    cid_to_uni: Dict[int, int],
    value_yx: Dict[str, Tuple[float, float]],
) -> str:
    """Static labels only — never donor value faces (those become unused glyphs)."""

    def is_value(mx: float, my: float) -> bool:
        for _key, (y, x) in value_yx.items():
            if abs(my - y) <= 1.5 and abs(mx - x) <= 3.0:
                return True
        return False

    text = []
    for m in _TM_RE.finditer(stream):
        mx, my = float(m.group(1)), float(m.group(2))
        if is_value(mx, my):
            continue
        tail = stream[m.end() : m.end() + 400]
        hit = _paint_hex_span(tail)
        if not hit:
            continue
        text.append(dec_hex(hit[2], cid_to_uni))
    return "".join(text)


def collect_stream_cids(stream: bytes) -> Set[int]:
    used: Set[int] = set()
    for m in _TM_RE.finditer(stream):
        tail = stream[m.end() : m.end() + 400]
        hit = _paint_hex_span(tail)
        if not hit:
            continue
        hx = hit[2].decode("ascii")
        for i in range(0, len(hx), 4):
            used.add(int(hx[i : i + 4], 16))
    return used


def _paint_hex_span(tail: bytes) -> Optional[Tuple[int, int, bytes, str]]:
    """Locate painted CID hex after Tm.

    Returns (start, end, joined_hex_ascii, kind) where kind is:
      - ``tj``     — Oracle/card compact ``<HHHH...> Tj``
      - ``tj_arr`` — Quartz phone ``[ <HHHH> 1 <HHHH> 1 ... ] TJ``

    Always take the *earliest* paint after this Tm. Preferring TJ anywhere in
    the lookahead window steals the next text object (phone labels → FO crash).
    """
    m_arr = re.search(rb"\[\s*((?:<[0-9A-Fa-f]+>\s*-?\d*\s*)+)\]\s*TJ", tail)
    m_tj = re.search(rb"<([0-9A-Fa-f]+)>\s*Tj", tail)
    cands: List[Tuple[int, int, int, bytes, str]] = []
    if m_arr:
        parts = re.findall(rb"<([0-9A-Fa-f]+)>", m_arr.group(1))
        if parts:
            cands.append(
                (m_arr.start(), m_arr.start(), m_arr.end(), b"".join(parts), "tj_arr")
            )
    if m_tj and len(m_tj.group(1)) >= 4:
        cands.append(
            (m_tj.start(), m_tj.start(1), m_tj.end(1), m_tj.group(1), "tj")
        )
    if not cands:
        m_bare = re.search(rb"Tf\s*<([0-9A-Fa-f]{4,})>", tail)
        if m_bare:
            return m_bare.start(1), m_bare.end(1), m_bare.group(1), "tj"
        return None
    cands.sort(key=lambda c: c[0])
    _pos, a, b, hx, kind = cands[0]
    return a, b, hx, kind


def _encode_paint(face: str, uni_to_cid: Dict[int, int], kind: str) -> Optional[bytes]:
    hx = enc_text(face, uni_to_cid)
    if not hx:
        return None
    if kind != "tj_arr":
        return hx
    # Rebuild Quartz TJ array: [ <CID> 1 <CID> 1 ... ] TJ
    chunks = [hx[i : i + 4] for i in range(0, len(hx), 4)]
    body = b" ".join(b"<%s> 1" % c for c in chunks)
    return b"[ " + body + b" ] TJ"


def first_appearance_cps(stream: bytes, cid_to_uni: Dict[int, int]) -> List[int]:
    """Unicode in Tj/TJ order; CID 0 / .notdef skipped."""
    ordered: List[int] = []
    seen: set = set()
    for m in _TM_RE.finditer(stream):
        tail = stream[m.end() : m.end() + 400]
        hit = _paint_hex_span(tail)
        if not hit:
            continue
        _a, _b, hx_b, _kind = hit
        hx = hx_b.decode("ascii")
        for i in range(0, len(hx), 4):
            cid = int(hx[i : i + 4], 16)
            if cid == 0:
                continue
            uni = cid_to_uni.get(cid)
            if not uni:
                continue
            if uni == 0x20:
                uni = 0x00A0
            if uni in seen:
                continue
            seen.add(uni)
            ordered.append(uni)
    return ordered


def _label_face(raw: str) -> str:
    """Keep title trailing NBSP; strip it from «Сформирована» and other labels."""
    t = (raw or "").replace("\x00", "")
    if t.startswith("Сформирована"):
        return "Сформирована"
    stripped = t.rstrip(_NBSP + " ")
    if stripped.startswith("Квитанция"):
        face = t.rstrip(" ")
        if not face.endswith(_NBSP):
            face = stripped + _NBSP
        return face
    return stripped if stripped else _NBSP


def _value_key_at(
    mx: float, my: float, value_yx: Dict[str, Tuple[float, float]],
) -> Optional[str]:
    for key, (y, x) in value_yx.items():
        if abs(my - y) <= 1.5 and abs(mx - x) <= 3.0:
            return key
    return None


def planned_keyed_faces(
    stream: bytes,
    old_cid_to_uni: Dict[int, int],
    value_yx: Dict[str, Tuple[float, float]],
    values: Dict[str, str],
) -> List[Tuple[Optional[str], str]]:
    """(slot key or None, face) in content-stream Tm order."""
    out: List[Tuple[Optional[str], str]] = []
    for m in _TM_RE.finditer(stream):
        mx, my = float(m.group(1)), float(m.group(2))
        tail = stream[m.end() : m.end() + 400]
        hit = _paint_hex_span(tail)
        if not hit or len(hit[2]) < 4:
            continue
        key = _value_key_at(mx, my, value_yx)
        if key and key in values:
            out.append((key, values[key]))
            continue
        out.append((key, _label_face(dec_hex(hit[2], old_cid_to_uni))))
    return out


def planned_slot_faces(
    stream: bytes,
    old_cid_to_uni: Dict[int, int],
    value_yx: Dict[str, Tuple[float, float]],
    values: Dict[str, str],
) -> List[str]:
    """Final Unicode of every Tj. Title keeps trailing NBSP; «Сформирована» does not."""
    return [face for _key, face in planned_keyed_faces(
        stream, old_cid_to_uni, value_yx, values,
    )]


def _digits_in(text: str) -> set:
    return {ch for ch in text or "" if ch.isdigit()}


def amount_new_digits(keyed: List[Tuple[Optional[str], str]]) -> set:
    """digits(amount) − digits(everything before amount in stream)."""
    pre: set = set()
    amt = ""
    for key, face in keyed:
        if key == "amount":
            amt = face
            break
        pre |= _digits_in(face)
    return _digits_in(amt) - pre


def stable_amount_pin(keyed: List[Tuple[Optional[str], str]]) -> str:
    """One new digit at the amount CID slot, independent of the face amount.

    amount10 PASS was `у` + `4` + `RUR` because date ДД.ММ.ГГГГ ЧЧ:ММ had
    no 4 yet; 4/7/8/9 arrived from :SS / op / phone. Pin the smallest
    missing digit that the receipt actually paints, so 700 and 12450
    share the same FO instead of 7 vs 4 at that CID.
    """
    pre: set = set()
    painted: set = set()
    saw_amount = False
    for key, face in keyed:
        painted |= _digits_in(face)
        if key == "amount":
            saw_amount = True
            continue
        if not saw_amount:
            pre |= _digits_in(face)
    for d in "0123456789":
        if d not in pre and d in painted:
            return d
    return ""


def subset_cps_pinned(keyed: List[Tuple[Optional[str], str]]) -> List[int]:
    """CID order = first appearance in content-stream Tm/Tj order.

    Live @bankpdfbot: every PASS today had cmap digit-FO == stream digit-FO.
    Skipping date digits to pin amount→4 made 14.08 paint `1408…` while the
    font started `4879…` → FAKE. The same 14.08 face with paint-order cmap
    (`pass_middle_nul`, 75 689 / Т-Банк) already PASSed.
    """
    ordered: List[int] = []
    seen: set = set()

    def add_ch(ch: str) -> None:
        if ch in ("\n", "\r", "\t"):
            return
        cp = 0x00A0 if ch in (" ", _NBSP) else ord(ch)
        if cp in seen:
            return
        seen.add(cp)
        ordered.append(cp)

    for _key, face in keyed:
        for ch in face:
            add_ch(ch)
    if 0x00A0 not in seen:
        ordered.append(0x00A0)
    return ordered


_CS_PAD_KEYS = frozenset({
    "amount", "commission", "operation_num",
    "date_time", "date_formed",
    # Card orig sender has a spare trailing NBSP vs a 16-char MIR mask.
    # Steal it so 5-digit amounts («87 900 RUR ») keep match_cs.
    "sender_card",
})
# Trailing NBSP here is a visual tell (origs do not pad these).
# receiver: genuine Oracle FIO ends with exactly one NBSP. Extra FIO NBSP
# (match_cs / CS-bump) is a Deacon Detect FAKE tell even when Proton is ЧИСТО.
_CS_NO_PAD_KEYS = frozenset({
    "sbp_id", "phone", "account", "recipient_bank", "message",
    "receiver_card", "receiver",
})
# FIO / message / date_formed extras are Deacon FAKE tells.
# operation_num already has the orig trailing NBSP; extra CIDs here walk
# clone CS (5111) without touching FIO. Empty bump → GEN_NONE on 5111.
_CS_BUMP_KEYS = ("operation_num",)
# Live OnlyPDF: rewritten CS of exactly 5111 (10.08 donor length) → FAKE.
# Natural PASS lengths include 5087 (PSB OnlyPDF `alfa_sbp_215038.pdf`) and
# 5091 / 5107 / 5123. Extra operation_num NBSP to walk 5087→5091 is a
# Deacon tell (Платон / 990, 02.09.2026 live ❌).
# Clone tells: 5111 (10.08 donor). 5115 is natural Ак Барс + orig phone — ship it.
# 5151: June Ozon orig CS (`pdf.pdf` / `pdf (1).pdf`); РСХБ rewrite landed here.
# 5135: Сбербанк rewrite (June Sber orig 5115 + long FIO) — OnlyPDF FAKE,
# Proton ЧИСТО (Борислав / 6897).
# 5183: first slot after the old 5155..5182 walk; every Ozon gen landed here,
# then FAKE (Велимира / Иннокентий, Proton ЧИСТО).
_SBP_CS_CLONE_LEN = frozenset({
    5111, 5119, 5135, 5151,
    5155, 5159, 5163, 5167, 5171, 5175, 5179, 5183,
    # 5187: bump used to stop at <5187; Raiffeisen 16.08 landed here → Deacon FAKE.
    5187, 5191, 5203,
})
# 5115 used to be treated as 5111+1 FIO NBSP. Natural 5115 (Ак Барс + orig
# phone, FIO pad=1) must ship with orig «+7 (XXX) XXX-XX-XX»; compacting
# the phone to dodge 5115 is itself a Deacon tell.
_SBP_CS_MIN = 5087
# Donor CS lengths that Deacon PASS gens actually used (not Ozon-late 5155 ridge).
_SAFE_DONOR_CS = frozenset({5087, 5091, 5095, 5099, 5103, 5107, 5115, 5123, 5127, 5131})
# Proton HARD ALFA_ORACLE_CONTENT_MIDGAP: card decoded /Contents
# must be an Oracle card body, not the 4140/4212 gap before SBP 5091.
_CARD_CS_HARD = frozenset({3413, 4152})


def _sbp_cs_need_bump(n: int) -> bool:
    if n < _SBP_CS_MIN or n in _SBP_CS_CLONE_LEN:
        return True
    # 15.08 Ozon-late orig is 5155; walk that ridge through 5187 (Deacon FAKE
    # landing). 5195 is a live WB PASS; WB orig CS 5203 is a clone tell.
    return 5155 <= n <= 5191


def _sync_rur_trailing_nbsp(
    faces: List[str],
    keys: List[Optional[str]],
    donor_faces: List[str],
) -> List[str]:
    """Donor Oracle amount/commission ends with RUR + NBSP in the same CID width.

    Formatter faces may land ``867 546 RUR`` (11) while the shell paints
    ``14 000 RUR\\xa0`` (11) — equal length, missing typography NBSP →
    rur-nbsp-asymmetry vs commission and Proton HARD.
    """
    out = list(faces)
    for i, key in enumerate(keys):
        if key not in ("amount", "commission"):
            continue
        donor = (donor_faces[i] if i < len(donor_faces) else "") or ""
        if not donor.endswith(_NBSP):
            continue
        if not donor.rstrip(_NBSP).endswith("RUR"):
            continue
        face = out[i] or ""
        core = face.rstrip(_NBSP)
        if core.endswith("RUR") and not face.endswith(_NBSP):
            out[i] = core + _NBSP
    return out


def _trim_face_to_orig_cids(face: str, key: Optional[str], orig_n: int) -> str:
    """Drop formatter-only trailing NBSP so the slot can stay donor-sized.

    Never eat the required RUR trailing NBSP. FIO keeps exactly one trailing
    NBSP — extras are a live FAKE tell, not a CS-matching tool.
    """
    t = face or ""
    while len(t) > orig_n and t.endswith(_NBSP):
        core = t[:-1]
        if key in ("amount", "commission") and core.endswith("RUR"):
            break
        if key == "receiver":
            # Keep the semantic trailing NBSP; drop match_cs extras.
            if not core.endswith(_NBSP):
                break
        t = core
    return t


def _cap_receiver_one_nbsp(faces: List[str], keys: List[Optional[str]]) -> List[str]:
    """Oracle FIO: exactly one trailing NBSP (origs / Deacon PASS gens)."""
    out = list(faces)
    for i, key in enumerate(keys):
        if key != "receiver" or i >= len(out):
            continue
        core = (out[i] or "").rstrip(_NBSP)
        if core:
            out[i] = core + _NBSP
    return out


def _pad_faces_to_donor_cids(
    faces: List[str],
    orig_cids: List[int],
    keys: List[Optional[str]],
) -> Optional[List[str]]:
    """Keep decoded /Contents length = donor by trailing NBSP only.

    Short amount/bank vs long FIO must net to the same CID count as the
    shell. Never )Tj / ET space pads.
    """
    if not faces or len(faces) != len(orig_cids) or len(faces) != len(keys):
        return None
    faces = [
        _trim_face_to_orig_cids(face, key, orig_n)
        for face, key, orig_n in zip(faces, keys, orig_cids)
    ]
    sem = [len(f) for f in faces]
    eligible = [
        i for i, key in enumerate(keys)
        if key not in _CS_NO_PAD_KEYS and (
            key in _CS_PAD_KEYS or (not key and faces[i].endswith(_NBSP))
        )
    ]
    targets = []
    for i, key in enumerate(keys):
        if i in eligible:
            targets.append(max(sem[i], orig_cids[i]))
        else:
            targets.append(sem[i])
    total_t, total_o = sum(targets), sum(orig_cids)
    if total_t < total_o:
        if not eligible:
            return None
        targets[eligible[-1]] += total_o - total_t
    elif total_t > total_o:
        need = total_t - total_o
        for i in reversed(eligible):
            spare = targets[i] - sem[i]
            take = min(spare, need)
            targets[i] -= take
            need -= take
            if need == 0:
                break
        if need:
            return None
    out: List[str] = []
    for i, face in enumerate(faces):
        n = targets[i] - len(face)
        if n < 0:
            return None
        out.append(face + (_NBSP * n))
    return out


def rewrite_stream(
    stream: bytearray,
    old_cid_to_uni: Dict[int, int],
    uni_to_cid: Dict[int, int],
    value_yx: Dict[str, Tuple[float, float]],
    values: Dict[str, str],
    *,
    match_cs: bool = True,
) -> Optional[str]:
    """Re-encode every Tm paint run (Oracle Tj or Quartz TJ array)."""

    rows: List[Tuple[int, int, bytes, str, Optional[str], str]] = []
    for m in _TM_RE.finditer(bytes(stream)):
        mx, my = float(m.group(1)), float(m.group(2))
        tail = bytes(stream)[m.end() : m.end() + 400]
        hit = _paint_hex_span(tail)
        if not hit or len(hit[2]) < 4:
            continue
        rel_a, rel_b, old_hx, kind = hit
        start = m.end() + rel_a
        end = m.end() + rel_b
        key = _value_key_at(mx, my, value_yx)
        if key and key in values:
            face = values[key]
        else:
            face = _label_face(dec_hex(old_hx, old_cid_to_uni))
        rows.append((start, end, old_hx, kind, key, face))
    orig_len = len(stream)
    faces = [row[5] for row in rows]
    if rows and all(row[3] == "tj" for row in rows):
        orig_cids = [len(row[2]) // 4 for row in rows]
        keys = [row[4] for row in rows]
        donor_faces = [dec_hex(row[2], old_cid_to_uni) for row in rows]
        faces = _sync_rur_trailing_nbsp(faces, keys, donor_faces)
        if match_cs:
            padded = _pad_faces_to_donor_cids(faces, orig_cids, keys)
            if padded is None:
                return "text overflow"
            faces = padded
        else:
            faces = [
                _trim_face_to_orig_cids(face, key, orig_n)
                for face, key, orig_n in zip(faces, keys, orig_cids)
            ]
        faces = _cap_receiver_one_nbsp(faces, keys)
    def _apply(face_list: List[str]) -> Optional[str]:
        edits: List[Tuple[int, int, bytes]] = []
        for (start, end, _old_hx, kind, _key, _face), face in zip(rows, face_list):
            painted = _encode_paint(face, uni_to_cid, kind)
            if not painted:
                miss = [
                    ch for ch in face
                    if (0x00A0 if ch in (" ", _NBSP) else ord(ch)) not in uni_to_cid
                ]
                return f"glyph mismatch {''.join(miss[:8]) or face[:12]!r}"
            edits.append((start, end, painted))
        stream[:] = orig
        for start, end, painted in reversed(edits):
            stream[start:end] = painted
        return None

    orig = bytes(stream)
    why = _apply(faces)
    if why:
        return why
    if match_cs and rows and all(row[3] == "tj" for row in rows) and len(stream) != orig_len:
        return f"glyph mismatch cs-len:{len(stream)}!={orig_len}"
    if (
        not match_cs
        and rows
        and all(row[3] == "tj" for row in rows)
        and _sbp_cs_need_bump(len(stream))
    ):
        bump_at = None
        for bump_key in _CS_BUMP_KEYS:
            bump_at = next(
                (
                    i
                    for i, (_s, _e, _hx, kind, key, _face) in enumerate(rows)
                    if key == bump_key and kind == "tj"
                ),
                None,
            )
            if bump_at is not None:
                break
        keys = [row[4] for row in rows]
        extra = 0
        while bump_at is not None and _sbp_cs_need_bump(len(stream)) and extra < 2:
            extra += 1
            bumped = _cap_receiver_one_nbsp(list(faces), keys)
            bumped[bump_at] = bumped[bump_at] + (_NBSP * extra)
            why = _apply(bumped)
            if why:
                return why
        if _sbp_cs_need_bump(len(stream)):
            for alt_key in _CS_BUMP_KEYS:
                alt_at = next(
                    (
                        i
                        for i, (_s, _e, _hx, kind, key, _face) in enumerate(rows)
                        if key == alt_key and kind == "tj"
                    ),
                    None,
                )
                if alt_at is None:
                    continue
                for extra2 in range(1, 3):
                    bumped = _cap_receiver_one_nbsp(list(faces), keys)
                    bumped[alt_at] = bumped[alt_at] + (_NBSP * extra2)
                    why = _apply(bumped)
                    if why:
                        break
                    if not _sbp_cs_need_bump(len(stream)):
                        break
                if not _sbp_cs_need_bump(len(stream)):
                    break
    return None


def fo_cid_1_to_n(
    stream: bytes,
    cid_to_uni: Dict[int, int],
) -> str:
    """FO CID 1..N after final face substitution. Empty = closed.

    CID 0 is .notdef / unpainted. No ToUnicode U+0000 residues.
    """
    nuls = [c for c, u in cid_to_uni.items() if c and u == 0]
    if nuls:
        return f"glyph mismatch tounicode-nul:{nuls[:8]}"
    fo = first_appearance_cps(stream, cid_to_uni)
    if not fo:
        return "glyph mismatch fo-empty"
    n = len(fo)
    mapped = sorted(c for c, u in cid_to_uni.items() if c and u)
    if mapped != list(range(1, n + 1)):
        return f"glyph mismatch fo-span:{mapped[:16]} n={n}"
    for i, cp in enumerate(fo, start=1):
        if cid_to_uni.get(i) != cp:
            return f"glyph mismatch fo-order:{i}"
    extra = unused_printable_cids(stream, cid_to_uni)
    if extra:
        return f"glyph mismatch unused:{extra[:8]}"
    used = collect_stream_cids(stream)
    extra_mapped = sorted(set(cid_to_uni) - used)
    if extra_mapped != [0]:
        return f"glyph mismatch mapped-used:{extra_mapped[:8]}"
    return ""


def unused_printable_cids(
    stream: bytes,
    cid_to_uni: Dict[int, int],
) -> List[int]:
    """Printable ToUnicode CIDs that never appear in Tj/TJ."""
    painted = collect_stream_cids(stream)
    extra = []
    for cid, uni in sorted(cid_to_uni.items()):
        if cid in painted or cid == 0:
            continue
        if uni in (0, 0x00A0) or uni <= 0x20:
            continue
        extra.append(cid)
    return extra


def _orphan_gids(ttf: bytes, used_cids: Set[int]) -> List[int]:
    from io import BytesIO

    from fontTools.ttLib import TTFont

    tt = TTFont(BytesIO(ttf))
    try:
        order = list(tt.getGlyphOrder())
        n = int(tt["maxp"].numGlyphs)
        required: Set[int] = {0} | {c for c in used_cids if 0 <= c < n}
        extra: Set[int] = set()
        for gid in list(required):
            _glyf_component_gids(tt, gid, extra)
        required |= extra
        orphans = []
        for gid in range(n):
            if gid not in required:
                orphans.append(gid)
        return orphans
    finally:
        tt.close()


def tight_font_assertions(pdf: bytes) -> Dict[str, object]:
    """CID/ToUnicode/glyf closure for C1_no_unused_font."""
    from alfa_font_extend import _ff2_read_decompressed, _load_font_xrefs_from_bytes

    ctx = AlfaOrigContext()
    if not ctx.load_bytes(pdf):
        raise RuntimeError("tight assertions: cannot load pdf")
    used = collect_stream_cids(bytes(ctx.stream))
    mapped = set(ctx.cid_to_uni)
    extra_mapped = sorted(mapped - used)
    missing_mapped = sorted(used - mapped)
    unused_chars = []
    for cid in unused_printable_cids(bytes(ctx.stream), ctx.cid_to_uni):
        uni = ctx.cid_to_uni.get(cid) or 0
        unused_chars.append(chr(uni) if uni >= 32 else f"U+{uni:04X}")
    nul_mapped = sorted(c for c, u in ctx.cid_to_uni.items() if u == 0)
    refs = _load_font_xrefs_from_bytes(pdf)
    if not refs:
        raise RuntimeError("tight assertions: no font xrefs")
    ff2 = _ff2_read_decompressed(pdf, refs["ff2"])
    orphans = _orphan_gids(ff2, used)
    ok = (
        extra_mapped == [0]
        and missing_mapped == []
        and unused_chars == []
        and nul_mapped == []
        and orphans == []
    )
    return {
        "extra_mapped_cids": extra_mapped,
        "missing_mapped_cids": missing_mapped,
        "unused_printable_chars": unused_chars,
        "nul_mapped_cids": nul_mapped,
        "orphan_gids": orphans,
        "ok": ok,
    }


def _force_length1(pdf: bytearray, ff2_xref: int, length1: int) -> bool:
    """Set /Length1 on the FontFile2 dict to the decoded TTF size."""
    from tbank_orig_mode import find_object_range
    from tbank_sbp_stealth import _replace_byte_range_and_rebuild

    want = f"/Length1 {int(length1)}".encode("ascii")
    rng = find_object_range(bytes(pdf), ff2_xref)
    if rng:
        obj = bytes(pdf[rng[0]:rng[1]])
        if want in obj:
            return True
        new_obj = re.sub(rb"/Length1\s+\d+", want, obj, count=1)
        if new_obj != obj:
            if len(new_obj) == len(obj):
                pdf[rng[0]:rng[1]] = new_obj
                return True
            patched = _replace_byte_range_and_rebuild(bytes(pdf), rng[0], rng[1], new_obj)
            if patched is None:
                return False
            pdf[:] = patched
            return True
    from alfa_font_extend import _patch_fontfile2_length1

    return _patch_fontfile2_length1(pdf, ff2_xref, length1)


def _rebuild_tounicode(old_tu: bytes, cid_to_uni: Dict[int, int]) -> bytes:
    """Oracle BI CMap — no PS-Adobe wrapper, `/Ordering(UCS)` glued, `end end`.

    Live originals start with `/CIDInit` not `%!PS-Adobe`. The PS wrapper was
    our fallback when regex missed `beginbfchar` after an xref rebuild.
    """
    _ = old_tu
    pairs = []
    for cid, uni in sorted(cid_to_uni.items()):
        if cid == 0:
            pairs.append((0, uni or 0x003F))
        elif uni == 0:
            pairs.append((cid, 0))
        elif uni and uni != 0xFFFF:
            pairs.append((cid, uni))
    if not any(cid == 0 for cid, _uni in pairs):
        pairs = [(0, 0x003F)] + pairs
    nl = "\r\n"
    body = "".join(f"<{cid:04X}> <{uni:04X}>{nl}" for cid, uni in pairs)
    text = (
        f"/CIDInit /ProcSet findresource begin{nl}"
        f"12 dict begin begincmap /CIDSystemInfo{nl}"
        f"<< /Registry (Oracle) /Ordering(UCS) /Supplement 0 >> def{nl}"
        f"/CMapName /Oracle-Identity-UCS def{nl}"
        f"1 begincodespacerange{nl}"
        f"<0000> <FFFF>{nl}"
        f"endcodespacerange{nl}"
        f"{len(pairs)} beginbfchar{nl}"
        f"{body}endbfchar{nl}"
        f"endcmap{nl}"
        f"CMapName currentdict /CMap defineresource pop{nl}"
        f"end end{nl}"
    )
    return text.encode("latin1")


def _pin_oracle_fontbbox(pdf: bytearray, ff2_xref: int) -> bool:
    """Oracle FontDescriptor /FontBBox is frozen, not recomputed from head."""
    from tbank_orig_mode import find_object_range
    from tbank_sbp_stealth import _replace_byte_range_and_rebuild

    x0, y0, x1, y1 = _ORACLE_FONTBBOX
    bbox_s = _ORACLE_FONTBBOX_PDF
    ok_any = False
    for xref in range(1, 40):
        rng = find_object_range(bytes(pdf), xref)
        if not rng:
            continue
        obj = bytes(pdf[rng[0] : rng[1]]).decode("latin1", "replace")
        if f"/FontFile2 {ff2_xref} 0 R" not in obj or "/FontBBox" not in obj:
            continue
        new_obj = re.sub(
            r"/FontBBox\s*\[\s*-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s*\]",
            bbox_s,
            obj,
            count=1,
        )
        if new_obj == obj:
            ok_any = True
            continue
        nb = new_obj.encode("latin1")
        if len(nb) == rng[1] - rng[0]:
            pdf[rng[0] : rng[1]] = nb
        else:
            patched = _replace_byte_range_and_rebuild(bytes(pdf), rng[0], rng[1], nb)
            if patched is None:
                return False
            pdf[:] = patched
        ok_any = True
    return ok_any


def _new_prefix(
    pdf: bytes,
    seed: bytes,
    ttf: bytes,
    cid_to_uni: Dict[int, int],
) -> bytes:
    """Fresh six-letter tag bound to this FontFile2 + CID/ToUnicode identity."""
    from alfa_sbp_stealth import _corpus_subset_tags, _sent_prefix_map

    forbidden = set(_corpus_subset_tags()) | set(_sent_prefix_map())
    cid_blob = b"".join(
        int(cid).to_bytes(2, "big") + int(cp).to_bytes(4, "big")
        for cid, cp in sorted(cid_to_uni.items())
    )
    material = hashlib.sha256(ttf + cid_blob + seed).digest()
    for counter in range(4096):
        digest = hashlib.sha256(material + counter.to_bytes(4, "big")).digest()
        prefix = bytes(65 + (b % 26) for b in digest[:6])
        if prefix not in forbidden:
            return prefix
    raise ValueError("no unique subset prefix")


def apply_prefix(pdf: bytes, prefix: bytes) -> bytes:
    """Rewrite subset tags. Keep total BaseFont byte length (xref-safe)."""
    out = pdf
    # Oracle/card: /ABCDEF+Tahoma → /NEWTAG+Tahoma (same length)
    for old in set(re.findall(rb"/([A-Z]{6})\+Tahoma", out)):
        out = out.replace(old + b"+Tahoma", prefix + b"+Tahoma")
    # Quartz phone: /AAAAAB+font000000002ff81462 — only swap the 6-letter tag
    for old_tag, old_name in set(re.findall(rb"/([A-Z]{6})\+(font[0-9a-f]+)", out)):
        out = out.replace(old_tag + b"+" + old_name, prefix + b"+" + old_name)
    return out


def _stream_span_by_length(pdf: bytes, xref: int) -> Optional[Tuple[int, int]]:
    """Stream bounds from /Length. Never scan endstream inside binary FF2."""
    needle = f"\n{xref} 0 obj".encode("ascii")
    pos = pdf.find(needle)
    if pos < 0:
        pos = pdf.find(f"{xref} 0 obj".encode("ascii"))
        if pos < 0:
            return None
    s = pdf.find(b"stream", pos)
    if s < 0 or s - pos > 800:
        return None
    dict_part = pdf[pos:s]
    matches = list(re.finditer(rb"/Length\s+(\d+)", dict_part))
    if not matches:
        return None
    length = int(matches[0].group(1))
    cs = s + 6
    if pdf[cs : cs + 2] == b"\r\n":
        cs += 2
    elif pdf[cs : cs + 1] == b"\n":
        cs += 1
    return cs, cs + length


def _install_ff2_rebuild(
    pdf: bytearray, ff2_xref: int, ttf: bytes, *, flate_level: int = 6,
) -> bool:
    """Always flate + Length/xref rebuild. No entropy tail on the TTF."""
    from alfa_orig_mode import _unreproducible_flate
    from tbank_sbp_stealth import _patch_length_and_rebuild

    pos = _stream_span_by_length(bytes(pdf), ff2_xref)
    if not pos:
        return False
    cs, ce = pos
    compressed = _unreproducible_flate(ttf, flate_level)
    patched = _patch_length_and_rebuild(pdf, cs, ce, compressed)
    if patched is None:
        return False
    pdf[:] = patched
    return True


def install_font(
    pdf: bytes,
    ttf: bytes,
    cid_to_uni: Dict[int, int],
    cid_w: Dict[int, int],
    prefix: bytes,
    *,
    dialect: str = "oracle",
) -> Tuple[Optional[bytes], str]:
    from alfa_font_extend import (
        _ff2_read_decompressed,
        _load_font_xrefs_from_bytes,
        _ot_checksum_matches,
        _patch_tu_decompressed,
        _tu_read_decompressed,
    )

    dial = (dialect or "oracle").strip().lower()
    if not sfnt_has_exact_aligned_end(ttf):
        return None, (
            "glyph mismatch sfnt-tail:"
            f"{len(ttf)}!={sfnt_aligned_end(ttf)}"
        )
    if not _ot_checksum_matches(ttf):
        return None, "glyph mismatch ot-csa"
    buf = bytearray(apply_prefix(pdf, prefix))
    refs = _load_font_xrefs_from_bytes(bytes(buf))
    if not refs:
        return None, "xref/Length mismatch font-xrefs"
    # Quartz phone donors compress FontFile2 at zlib level 1 (hdr 7801).
    ff2_level = 1 if dial == "quartz" else 6
    if not _install_ff2_rebuild(buf, refs["ff2"], ttf, flate_level=ff2_level):
        return None, "xref/Length mismatch FontFile2"
    try:
        refs = _load_font_xrefs_from_bytes(bytes(buf)) or refs
        old_tu = _tu_read_decompressed(bytes(buf), refs["tu"])
        if dial == "quartz":
            new_tu = _rebuild_tounicode_quartz(old_tu, cid_to_uni)
        else:
            new_tu = _rebuild_tounicode(old_tu, cid_to_uni)
        if not _patch_tu_decompressed(buf, refs["tu"], new_tu):
            return None, "xref/Length mismatch ToUnicode"
        refs = _load_font_xrefs_from_bytes(bytes(buf)) or refs
    except Exception as exc:
        return None, f"xref/Length mismatch after-tu:{exc}"
    try:
        from tbank_orig_mode import find_object_range
        from tbank_sbp_stealth import _replace_byte_range_and_rebuild

        rng = find_object_range(bytes(buf), refs["cid"])
        if not rng:
            return None, "width mismatch no-cidfont"
        obj = bytes(buf[rng[0] : rng[1]]).decode("latin1", "replace")
        if dial == "quartz":
            # Keep `/W N 0 R`; rewrite the indirect array object in place.
            m_ind = re.search(r"/W\s+(\d+)\s+0\s+R", obj)
            if not m_ind:
                return None, "width mismatch /W-ind-miss"
            w_xref = int(m_ind.group(1))
            w_rng = find_object_range(bytes(buf), w_xref)
            if not w_rng:
                return None, "width mismatch /W-obj"
            w_obj = bytes(buf[w_rng[0] : w_rng[1]]).decode("latin1", "replace")
            m_arr = re.search(r"\[\s*1\s*\[.*?\]\s*\]", w_obj, re.S)
            if not m_arr:
                m_arr = re.search(r"\[.*\]", w_obj, re.S)
            if not m_arr:
                return None, "width mismatch /W-body"
            from io import BytesIO

            from fontTools.ttLib import TTFont

            tt = TTFont(BytesIO(ttf))
            try:
                upem = int(tt["head"].unitsPerEm or 2048)
                order = tt.getGlyphOrder()
                q_w: Dict[int, int] = {}
                for cid in sorted(c for c in cid_w if c >= 1):
                    if cid >= len(order):
                        continue
                    aw = int(tt["hmtx"].metrics[order[cid]][0])
                    q_w[cid] = _pdf_w_quartz(aw, upem)
            finally:
                tt.close()
            new_body = _serialize_w_quartz(q_w).decode("ascii")
            new_w_obj = w_obj[: m_arr.start()] + new_body + w_obj[m_arr.end() :]
            patched = _replace_byte_range_and_rebuild(
                bytes(buf), w_rng[0], w_rng[1], new_w_obj.encode("latin1"),
            )
            if patched is None:
                return None, "width mismatch /W"
            buf = bytearray(patched)
        else:
            w_full = _serialize_w_oracle(cid_w).decode("ascii")
            br = None
            m_ind = re.search(r"/W\s+(\d+)\s+0\s+R", obj)
            if m_ind:
                br = (m_ind.start(), m_ind.end())
            else:
                m_w = re.search(r"/W\s*\[", obj)
                if m_w:
                    i = m_w.end() - 1
                    depth = 0
                    while i < len(obj):
                        if obj[i] == "[":
                            depth += 1
                        elif obj[i] == "]":
                            depth -= 1
                            if depth == 0:
                                br = (m_w.start(), i + 1)
                                break
                        i += 1
            if not br:
                return None, "width mismatch /W-miss"
            new_obj = obj[: br[0]] + w_full + obj[br[1] :]
            patched = _replace_byte_range_and_rebuild(
                bytes(buf), rng[0], rng[1], new_obj.encode("latin1"),
            )
            if patched is None:
                return None, "width mismatch /W"
            buf = bytearray(patched)
        refs = _load_font_xrefs_from_bytes(bytes(buf)) or refs
        landed = _ff2_read_decompressed(bytes(buf), refs["ff2"])
    except Exception as exc:
        return None, f"xref/Length mismatch after-w:{exc}"
    if not landed or not sfnt_has_exact_aligned_end(landed):
        return None, (
            "glyph mismatch landed-sfnt-tail:"
            f"{len(landed)}!={sfnt_aligned_end(landed)}"
        )
    if not _ot_checksum_matches(landed):
        return None, "glyph mismatch landed-ot"
    if not _force_length1(buf, refs["ff2"], len(landed)):
        return None, "xref/Length mismatch Length1"
    refs = _load_font_xrefs_from_bytes(bytes(buf)) or refs
    if dial != "quartz":
        _pin_oracle_fontbbox(buf, refs["ff2"])
    return bytes(buf), ""


def emit_onto_shell(
    shell_pdf: bytes,
    values: Dict[str, str],
    value_yx: Dict[str, Tuple[float, float]],
    seed: bytes,
    *,
    profile: str = "oracle",
    match_cs: bool = True,
    hmtx_uniq_floor: int = 0,
) -> Tuple[Optional[bytes], str]:
    """Full font subset + semantic text rewrite on an Oracle page shell.

    Digit CIDs follow first paint in the stream (date, then amount, …).
    Live profile is Oracle-native (no ToUnicode NULs, CSA pin, FO 1..N).

    NEG 3a (docs/specimen_alfa_sbp_NEG_3a24266b.pdf) already had green
    FO/charset/SFNT/CSA and matching op/SBP — do not treat that live ❌
    as a field bug. Byte-diff remaining: FontFile2, Content, ToUnicode/CID.
    """
    ctx = AlfaOrigContext()
    if not ctx.load_bytes(shell_pdf):
        return None, "xref/Length mismatch shell"
    old_c2u = dict(ctx.cid_to_uni)
    orig_stream = bytes(ctx.stream)
    keyed = planned_keyed_faces(orig_stream, old_c2u, value_yx, values)
    faces = [face for _key, face in keyed]
    need: List[int] = []
    for ch in "".join(faces):
        if ch in ("\n", "\r", "\t"):
            continue
        need.append(0x00A0 if ch in (" ", _NBSP) else ord(ch))
    miss = missing_chars("".join(chr(cp) if cp != 0x00A0 else _NBSP for cp in need))
    if miss:
        try:
            from alfa_oracle_master import ensure_parent_covers

            ensure_parent_covers("".join(miss))
            miss = missing_chars(
                "".join(chr(cp) if cp != 0x00A0 else _NBSP for cp in need)
            )
        except Exception as exc:
            logger.warning("Alfa emit parent-cover failed: %s", exc)
    if miss:
        return None, f"glyph mismatch {''.join(dict.fromkeys(miss))}"

    pin = stable_amount_pin(keyed)
    natural = amount_new_digits(keyed)
    logger.info(
        "Alfa emit amount new_digits=%s pin=%s",
        "".join(sorted(natural)) or "∅",
        pin or "∅",
    )
    cps = subset_cps_pinned(keyed)
    ttf = b""
    u2c: Dict[int, int] = {}
    c2u: Dict[int, int] = {}
    widths: Dict[int, int] = {}
    stream = bytearray(orig_stream)
    dial = "quartz" if (profile or "").strip().lower() in ("quartz", "phone") else "oracle"
    subset_profile = "oracle" if dial == "quartz" else (profile or "oracle")
    for pass_i in range(4):
        try:
            if dial == "quartz":
                ttf, u2c, c2u, widths = build_subset(
                    cps, seed + bytes([pass_i]), profile=subset_profile,
                )
            else:
                ttf, u2c, c2u, widths = subset_from_origs(
                    cps, hmtx_uniq_floor=hmtx_uniq_floor,
                )
        except Exception as exc:
            return None, f"glyph mismatch {exc}"
        stream = bytearray(orig_stream)
        why = rewrite_stream(
            stream, old_c2u, u2c, value_yx, values, match_cs=match_cs,
        )
        if why:
            return None, why
        if match_cs and dial != "quartz" and len(stream) != len(orig_stream):
            return None, f"glyph mismatch cs-len:{len(stream)}!={len(orig_stream)}"
        extra = unused_printable_cids(bytes(stream), c2u)
        if extra:
            extra_uni = {c2u[c] for c in extra if c in c2u and c2u[c]}
            cps = [cp for cp in cps if cp not in extra_uni]
            if 0x00A0 not in cps:
                cps.append(0x00A0)
            continue
        why_fo = fo_cid_1_to_n(bytes(stream), c2u)
        if why_fo:
            return None, why_fo
        break
    else:
        return None, "glyph mismatch unused-printable"

    from alfa_font_extend import _ff2_read_decompressed, _load_font_xrefs_from_bytes

    # Same face as the Oracle shell → same file. New prefix/ID would change
    # SHA and the live checker treats that as a tampered original.
    refs0 = _load_font_xrefs_from_bytes(shell_pdf)
    if refs0 and dial != "quartz":
        shell_ff2 = _ff2_read_decompressed(shell_pdf, refs0["ff2"])
        if shell_ff2 == ttf and bytes(stream) == orig_stream:
            return shell_pdf, ""

    prefix = _new_prefix(shell_pdf, seed, ttf, c2u)
    try:
        installed, why = install_font(
            ctx.pdf_bytes, ttf, c2u, widths, prefix, dialect=dial,
        )
    except Exception as exc:
        return None, f"xref/Length mismatch install:{exc}"
    if installed is None:
        return None, why or "font/prefix collision"
    pos_ctx = AlfaOrigContext()
    if not pos_ctx.load_bytes(installed):
        return None, "xref/Length mismatch reload"
    pos_ctx.stream = stream
    result = pos_ctx.commit_rebuild()
    if result is None:
        return None, "xref/Length mismatch content"
    # Content rebuild can shift objects — pin /Length1 to landed decoded TTF.
    from alfa_font_extend import _ff2_read_decompressed, _load_font_xrefs_from_bytes

    refs = _load_font_xrefs_from_bytes(result)
    if not refs:
        return None, "xref/Length mismatch final-xrefs"
    landed = _ff2_read_decompressed(result, refs["ff2"])
    if not landed:
        return None, "glyph mismatch landed-empty"
    if not sfnt_has_exact_aligned_end(landed):
        return None, (
            "glyph mismatch landed-sfnt-tail:"
            f"{len(landed)}!={sfnt_aligned_end(landed)}"
        )
    buf = bytearray(result)
    if not _force_length1(buf, refs["ff2"], len(landed)):
        return None, "xref/Length mismatch Length1"
    refs = _load_font_xrefs_from_bytes(bytes(buf)) or refs
    if dial != "quartz":
        _pin_oracle_fontbbox(buf, refs["ff2"])
    return bytes(buf), ""


def _face_spec_invariants(pdf: bytes, ctx: AlfaOrigContext, chan: str) -> str:
    """alfa_v2 2.1.2 HARD that is visible on the face / content stream.

    ET 1–10, operation[3:9]=DDMMYY, RUR trailing-NBSP symmetry, MIR BIN,
    last4 ABAB, phone DEF 9xx, phone masked-name/initials.
    """
    import fitz

    stream = bytes(ctx.stream)
    n_et = len(re.findall(rb" ET\b", stream))
    if 1 <= n_et <= 10:
        return f"layout mismatch et-whitespace:{n_et}"

    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        text = doc[0].get_text() if doc.page_count else ""
    finally:
        doc.close()
    raw = (text or "").replace("\u202f", " ")

    ops = re.findall(r"\b([A-Z]\d{15})\b", raw.upper())
    m_dt = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw.replace("\xa0", " "))
    if ops and m_dt:
        want = m_dt.group(1) + m_dt.group(2) + m_dt.group(3)[2:]
        for op in ops:
            if op[3:9] != want:
                return f"operation date-link:{op[3:9]}!={want}"

    rur_toks = list(
        re.finditer(
            r"(?:\d(?:[\d\u00a0\u202f]*\d)?)\u00a0RUR(\u00a0|\u202f)?",
            raw,
        )
    )
    if len(rur_toks) >= 2:
        trails = {m.group(1) is not None for m in rur_toks}
        if len(trails) > 1:
            return "glyph mismatch rur-nbsp-asymmetry"
    if re.search(r"(?<!\d)\d{4,}[\s\u00a0\u202f]*RUR", raw, re.IGNORECASE):
        return "amount typography-bare"

    if chan == "card":
        compact = re.sub(r"[\s\u00a0]", "", raw)
        for bin6, last4 in re.findall(r"(?<!\d)(\d{6})\*{4,8}(\d{4})(?!\d)", compact):
            # V1 MIR shells stay 220xxx; V2 «карта в другой банк» (Документ 6)
            # uses foreign BINs (e.g. T-Bank 437772). Accept any 6-digit BIN.
            if not bin6.isdigit():
                return f"card bin:{bin6}"
            if (
                last4[0] == last4[2]
                and last4[1] == last4[3]
                and last4[0] != last4[1]
            ):
                return f"card last4-abab:{last4}"

    visible = raw.replace("\xa0", " ")
    phone = re.search(r"\+7\s*\((\d{3})\)\s*\d{3}-\d{2}-\d{2}", visible)
    masked = re.search(r"(?<!\d)(\d{3})\*{3}(\d{4})(?!\d)", visible)
    def_code = ""
    if phone:
        def_code = phone.group(1)
    elif masked:
        def_code = masked.group(1)
    if def_code and not def_code.startswith("9"):
        return f"phone def:{def_code}"

    if chan == "phone" and masked:
        recipient = ""
        lines = visible.splitlines()
        for index, line in enumerate(lines):
            if not line.strip().lower().startswith("получатель"):
                continue
            inline = line.split(":", 1)
            if len(inline) == 2 and inline[1].strip():
                recipient = inline[1].strip()
            else:
                for value in lines[index + 1 : index + 5]:
                    if value.strip():
                        recipient = value.strip()
                        break
            break
        if recipient:
            initials = re.findall(r"[А-ЯЁ]\.", recipient)
            if "**" not in recipient:
                return "phone name-unmasked"
            if len(initials) == 1:
                return "phone initials"
    return ""


def java6_streams_invariant(pdf: bytes, expected: int = 6) -> str:
    """Require every Flate stream to equal Java Deflater(level=6) bytes."""
    import fitz

    from alfa_java_deflate import java_deflate

    doc = fitz.open(stream=pdf, filetype="pdf")
    checks = []
    try:
        for xref in range(1, doc.xref_length()):
            if "/FlateDecode" not in doc.xref_object(xref):
                continue
            raw = doc.xref_stream_raw(xref)
            decoded = doc.xref_stream(xref)
            canonical = java_deflate(decoded, 6)
            checks.append((xref, canonical is not None and raw == canonical))
    finally:
        doc.close()
    if len(checks) != expected:
        return f"stream mismatch java-count:{len(checks)}!={expected}"
    bad = [xref for xref, ok in checks if not ok]
    if bad:
        return f"stream mismatch java6:{bad}"
    return ""


def emit_invariants(pdf: bytes, *, channel: str = "sbp") -> str:
    """Empty string = live Oracle subset closed. Else rebuild reason.

    channel:
      sbp  — FIO NBSP + SBP id + debit account mask/checksum
      card — RUR trailing NBSP on amount/commission (no SBP face rules)
    """
    import struct

    from alfa_font_extend import (
        _ff2_read_decompressed,
        _head_table_offset,
        _load_font_xrefs_from_bytes,
        _sfnt_physical_tags,
    )

    ctx = AlfaOrigContext()
    if not ctx.load_bytes(pdf):
        return "xref/Length mismatch invariants"
    chan = (channel or "sbp").strip().lower()
    if chan == "card":
        n = len(bytes(ctx.stream))
        if n not in _CARD_CS_HARD:
            return f"glyph mismatch card-cs-midgap:{n}"
    # Quartz phone shells keep donor image Flate (7 streams); Oracle SBP/card = 6.
    if chan != "phone":
        why_java = java6_streams_invariant(pdf)
        if why_java:
            return why_java
    why_fo = fo_cid_1_to_n(bytes(ctx.stream), ctx.cid_to_uni)
    if why_fo and chan != "phone":
        return why_fo
    cmap_cids = sorted(ctx.cid_to_uni)
    if chan != "phone":
        if not cmap_cids or cmap_cids[0] != 0 or cmap_cids != list(range(cmap_cids[-1] + 1)):
            return f"glyph mismatch sparse-cid:{cmap_cids[:16]}"
        if ctx.cid_to_uni.get(0) != 0x003F:
            return f"glyph mismatch cid0:{ctx.cid_to_uni.get(0)!r}"
    formed_bad = False
    title_nbsp = False
    title_seen = False
    for m in _TM_RE.finditer(bytes(ctx.stream)):
        tail = bytes(ctx.stream)[m.end() : m.end() + 400]
        hit = _paint_hex_span(tail)
        if not hit:
            continue
        face = dec_hex(hit[2], ctx.cid_to_uni)
        if face.startswith("Сформирована") and face != "Сформирована":
            formed_bad = True
            break
        if face.startswith("Квитанция"):
            title_seen = True
            title_nbsp = face.endswith(_NBSP)
    if formed_bad:
        return "glyph mismatch formed-nbsp"
    if title_seen and not title_nbsp:
        return "glyph mismatch title-nbsp"
    why_face = _face_spec_invariants(pdf, ctx, chan)
    if why_face:
        return why_face
    refs = _load_font_xrefs_from_bytes(pdf)
    if not refs:
        return "xref/Length mismatch font-xrefs"
    ff2 = _ff2_read_decompressed(pdf, refs["ff2"])
    if not ff2:
        return "glyph mismatch empty-ff2"
    aligned_end = sfnt_aligned_end(ff2)
    if len(ff2) != aligned_end:
        return f"glyph mismatch sfnt-tail:{len(ff2)}!={aligned_end}"
    import fitz

    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        obj = doc.xref_object(refs["ff2"])
    finally:
        doc.close()
    m = re.search(r"/Length1\s+(\d+)", obj)
    if not m or int(m.group(1)) != len(ff2):
        got = m.group(1) if m else "?"
        return f"xref/Length mismatch Length1:{got}!={len(ff2)}"
    n = struct.unpack(">H", ff2[4:6])[0]
    tags = [ff2[12 + i * 16: 16 + i * 16].decode("latin-1") for i in range(min(n, 9))]
    if tags != _ORACLE_SFNT:
        return f"glyph mismatch table-order:{tags}"
    phys = _sfnt_physical_tags(ff2)
    if phys != _ORACLE_SFNT:
        return f"glyph mismatch phys-order:{phys}"
    off = _head_table_offset(ff2)
    if off is None:
        return "glyph mismatch no-head"
    csa = struct.unpack(">I", ff2[off + 8: off + 12])[0]
    if csa != _ORACLE_PIN_CSA:
        return f"glyph mismatch csa:{csa}!={_ORACLE_PIN_CSA}"

    prefixes = set(re.findall(rb"/([A-Z]{6})\+Tahoma", pdf))
    if chan != "phone":
        if len(prefixes) != 1:
            return f"glyph mismatch prefix-count:{len(prefixes)}"
        pref = next(iter(prefixes))
        from alfa_sbp_stealth import _corpus_prefix_ff2_map, _sent_prefix_map
        sha16 = hashlib.sha256(ff2).hexdigest()[:16]
        known = _corpus_prefix_ff2_map().get(pref) or _sent_prefix_map().get(pref)
        if known is not None and known != sha16:
            return f"glyph mismatch prefix-cogen:{pref.decode('ascii')}:{known}!={sha16}"

    if chan != "phone":
        fo_cps = first_appearance_cps(bytes(ctx.stream), ctx.cid_to_uni)
        try:
            rebuilt, _, _, _ = subset_from_origs(
                fo_cps,
                hmtx_uniq_floor=(
                    _ORACLE_SBP_HMTX_UNIQ_FLOOR if chan == "sbp" else 0
                ),
            )
        except Exception as exc:
            return f"glyph mismatch rebuild:{type(exc).__name__}"
        if rebuilt != ff2:
            return (
                "glyph mismatch rebuild-ff2:"
                f"{hashlib.sha256(rebuilt).hexdigest()[:16]}!="
                f"{hashlib.sha256(ff2).hexdigest()[:16]}"
            )

    if chan == "phone":
        # Quartz dialect: indirect compact /W, donor FontBBox, no Oracle W/CRLF.
        from tbank_orig_mode import find_object_range

        cid_rng = find_object_range(pdf, refs["cid"])
        if not cid_rng:
            return "width mismatch no-cidfont"
        cid_obj = pdf[cid_rng[0] : cid_rng[1]].decode("latin1", "replace")
        m_ind = re.search(r"/W\s+(\d+)\s+0\s+R", cid_obj)
        if not m_ind:
            return "width mismatch /W-ind-miss"
        if _ORACLE_FONTBBOX_PDF.encode("ascii") in pdf:
            return "glyph mismatch FontBBox-oracle-on-quartz"
        money = (("amount", 664.288, 35.45), ("commission", 621.394, 35.45))
        for key, y, x in money:
            got = ctx.extract_at(y, x) or ""
            if not got:
                for mtm in _TM_RE.finditer(bytes(ctx.stream)):
                    mx, my = float(mtm.group(1)), float(mtm.group(2))
                    if abs(my - y) > 1.5 or abs(mx - x) > 3.0:
                        continue
                    hit = _paint_hex_span(
                        bytes(ctx.stream)[mtm.end() : mtm.end() + 400]
                    )
                    if hit:
                        got = dec_hex(hit[2], ctx.cid_to_uni)
                    break
            idx = got.find("RUR")
            if idx < 0 or got[idx + 3 : idx + 4] != _NBSP:
                return f"glyph mismatch rur-nbsp:{key}"
        return ""

    raw_w = _raw_w_array(pdf, refs["cid"])
    if not raw_w.startswith(b"/W [ ") or not raw_w.endswith(b" ]"):
        return "width mismatch W-format"
    if re.search(rb"\d\[\d+(?: \d+)+\]", raw_w):
        return "width mismatch compact-W"
    n_w = len(re.findall(rb"\d+ \[\d+\]", raw_w))
    n_crlf = raw_w.count(b"\r\n")
    want_crlf = max(0, (n_w - 1) // 10)
    if n_crlf != want_crlf:
        return f"width mismatch W-crlf:{n_crlf}!={want_crlf}"
    if n_w != len(ctx.widths):
        return f"width mismatch W-count:{n_w}!={len(ctx.widths)}"
    from io import BytesIO

    from fontTools.ttLib import TTFont

    tt = TTFont(BytesIO(ff2))
    try:
        upem = int(tt["head"].unitsPerEm or 2048)
        order = tt.getGlyphOrder()
        n_glyphs = int(tt["maxp"].numGlyphs)
        n_hmtx = int(tt["hhea"].numberOfHMetrics)
        if n_hmtx != n_glyphs:
            return f"glyph mismatch hmtx-count:{n_hmtx}!={n_glyphs}"
        wmax = max(ctx.widths) if ctx.widths else -1
        if wmax != n_glyphs - 1 or n_w != n_glyphs:
            return f"width mismatch W-span:{wmax}+1/n={n_w} glyphs={n_glyphs}"
        tu_max = max(ctx.cid_to_uni)
        if tu_max >= n_glyphs:
            return f"glyph mismatch tu-past-glyf:{tu_max}>={n_glyphs}"
        check = sorted(ctx.widths)[:8] + sorted(ctx.widths)[-4:]
        for cid in check:
            if cid >= len(order):
                continue
            aw = int(tt["hmtx"].metrics[order[cid]][0])
            want = _pdf_w(aw, upem)
            got = int(ctx.widths[cid])
            if got != want:
                return f"width mismatch trunc:{cid}:{got}!={want}"
        if chan == "sbp" and len(ff2) >= _ORACLE_SBP_HMTX_UNIQ_MIN_FF2:
            uniq = _hmtx_uniq_positive(tt["hmtx"].metrics.values())
            if uniq < _ORACLE_SBP_HMTX_UNIQ_FLOOR:
                return (
                    f"glyph mismatch hmtx-uniq:{uniq}<{_ORACLE_SBP_HMTX_UNIQ_FLOOR}"
                )
    finally:
        tt.close()
    if _ORACLE_FONTBBOX_PDF.encode("ascii") not in pdf:
        return "glyph mismatch FontBBox"

    if chan in ("card", "phone"):
        # Keep coords local — avoid import cycle with channel modules.
        money = (
            (("amount", 664.3, 35.45), ("commission", 621.4, 35.45))
            if chan == "card"
            else (("amount", 664.288, 35.45), ("commission", 621.394, 35.45))
        )
        for key, y, x in money:
            got = ctx.extract_at(y, x) or ""
            if not got:
                # Quartz money paints are compact <hex>Tj; OrigContext._slot_at
                # only sees the first <…> — fall back to paint span decode.
                for m in _TM_RE.finditer(bytes(ctx.stream)):
                    mx, my = float(m.group(1)), float(m.group(2))
                    if abs(my - y) > 1.5 or abs(mx - x) > 3.0:
                        continue
                    hit = _paint_hex_span(bytes(ctx.stream)[m.end() : m.end() + 400])
                    if hit:
                        got = dec_hex(hit[2], ctx.cid_to_uni)
                    break
            idx = got.find("RUR")
            if idx < 0 or got[idx + 3 : idx + 4] != _NBSP:
                return f"glyph mismatch rur-nbsp:{key}"
        return ""

    from alfa_sbp_stealth import (
        SBP_COORDS,
        _ALFA_PAYER_BIK,
        _fio_trailing_nbsp_ok,
        _ru_account_checksum,
    )

    if not _fio_trailing_nbsp_ok(ctx):
        return "glyph mismatch fio-nbsp"
    sbp_face = ctx.extract_at(*SBP_COORDS["sbp_id"]) or ""
    if sbp_face.endswith(_NBSP):
        return "glyph mismatch sbp-nbsp"
    acct = re.sub(r"\D", "", ctx.extract_at(*SBP_COORDS["account"]) or "")
    if len(acct) == 20 and not acct.startswith("40817810"):
        return f"account mask:{acct}"
    if len(acct) == 20 and _ru_account_checksum(_ALFA_PAYER_BIK, acct) != 0:
        return f"account checksum alfa:{acct}"
    cs_n = len(bytes(ctx.stream))
    if _sbp_cs_need_bump(cs_n):
        return f"cs-len:{cs_n}"
    return ""
