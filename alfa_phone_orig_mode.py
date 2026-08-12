# -*- coding: utf-8 -*-
"""Alfa phone (iOS Quartz) orig-mode — donor content rewrite."""
from __future__ import annotations

import logging
import re
import zlib
from typing import Dict, List, Optional, Tuple

import fitz

logger = logging.getLogger(__name__)

_NBSP = "\u00a0"
# Quartz: "11 0 0 11 x y Tm" / "12 0 0 12 x y Tm"
_TM_RE = re.compile(
    rb"([\d.]+)\s+0\s+0\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+Tm"
)


def _parse_bfrange(tu: str) -> Dict[int, int]:
    """Oracle/Quartz ToUnicode beginbfrange → {cid: codepoint}."""
    out: Dict[int, int] = {}
    for block in re.finditer(r"\d+\s+beginbfrange(.*?)endbfrange", tu, re.S):
        body = block.group(1)
        for lo, hi, base in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", body
        ):
            start, stop, uni0 = int(lo, 16), int(hi, 16), int(base, 16)
            for off, cid in enumerate(range(start, stop + 1)):
                out[cid] = uni0 + off
        for lo, hi, arr in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[([^\]]*)\]", body
        ):
            start, stop = int(lo, 16), int(hi, 16)
            vals = re.findall(r"<([0-9A-Fa-f]+)>", arr)
            for off, cid in enumerate(range(start, stop + 1)):
                if off >= len(vals):
                    break
                out[cid] = int(vals[off], 16)
    # bfchar fallback
    for block in re.finditer(r"\d+\s+beginbfchar(.*?)endbfchar", tu, re.S):
        for src, dst in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block.group(1)
        ):
            out[int(src, 16)] = int(dst, 16)
    return out


def _find_stream_pos_for_xref(pdf: bytes, xref: int) -> Optional[Tuple[int, int]]:
    needle = f"\n{xref} 0 obj".encode()
    pos = pdf.find(needle)
    if pos < 0:
        needle2 = f"{xref} 0 obj".encode()
        pos = pdf.find(needle2)
        if pos < 0:
            return None
    s = pdf.find(b"stream", pos)
    if s < 0:
        return None
    cs = s + 6
    if pdf[cs : cs + 2] == b"\r\n":
        cs += 2
    elif pdf[cs : cs + 1] == b"\n":
        cs += 1
    es = pdf.find(b"endstream", cs)
    if es < 0:
        return None
    if pdf[es - 2 : es] == b"\r\n":
        ce = es - 2
    elif pdf[es - 1 : es] == b"\n":
        ce = es - 1
    else:
        ce = es
    return cs, ce


def _zlib_level_from_header(raw: bytes) -> int:
    header = raw[:2] if len(raw) >= 2 else b""
    if header[:2] == b"\x78\x01":
        return 1
    if header[:2] == b"\x78\x5e":
        return 5
    if header[:2] == b"\x78\x9c":
        return 6
    if header[:2] == b"\x78\xda":
        return 9
    return 6


def _oracle_near_flate(data: bytes, level: int = 6) -> bytes:
    from alfa_java_deflate import java_deflate

    lvl = level if 1 <= level <= 9 else 6
    out = java_deflate(data, level=lvl)
    if out is not None:
        try:
            if zlib.decompress(out) == data:
                return out
        except zlib.error:
            pass
    # Never zlib.compress here — mixes with Oracle image streams →
    # ALFA_MIXED_SERIALIZER_PROVENANCE. Caller must reject on None.
    logger.warning("Alfa phone java_deflate miss level=%d — no zlib fallback", lvl)
    return out if out is not None else b""


def force_java_canonical_all_flate(pdf: bytes) -> Optional[bytes]:
    """Re-encode every FlateDecode stream via Java Deflater(decoded).

    After glyph inject / content rewrite, corpus-canonical image bytes +
    recompressed font/content → Proton ALFA_MIXED_SERIALIZER_PROVENANCE.
    Uniform Java-canonical streams clear the mix (6/6 is_canonical).
    """
    import fitz
    from alfa_java_deflate import java_deflate
    from alfa_font_extend import _find_stream_pos_for_xref, _patch_length_and_rebuild

    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception:
        return None
    flate_xrefs: list[int] = []
    for xref in range(1, doc.xref_length()):
        try:
            if not doc.xref_is_stream(xref):
                continue
            obj = doc.xref_object(xref) or ""
            if "/FlateDecode" not in obj and "/Flate" not in obj:
                continue
            flate_xrefs.append(xref)
        except Exception:
            continue
    doc.close()

    out = bytearray(pdf)
    changed = False
    for xref in flate_xrefs:
        try:
            d = fitz.open(stream=bytes(out), filetype="pdf")
            dec = d.xref_stream(xref)
            raw = d.xref_stream_raw(xref) or b""
            d.close()
        except Exception:
            return None
        if not dec:
            continue
        lvl = _zlib_level_from_header(raw[:2]) if len(raw) >= 2 else 6
        comp = java_deflate(dec, level=lvl if 1 <= lvl <= 9 else 6)
        if comp is None:
            logger.error("force_java_canonical: java_deflate failed xref=%s", xref)
            return None
        try:
            if zlib.decompress(comp) != dec:
                logger.error("force_java_canonical: roundtrip fail xref=%s", xref)
                return None
        except zlib.error:
            return None
        if comp == raw:
            continue
        pos = _find_stream_pos_for_xref(bytes(out), xref)
        if not pos:
            logger.error("force_java_canonical: stream pos miss xref=%s", xref)
            return None
        patched = _patch_length_and_rebuild(bytearray(out), pos[0], pos[1], comp)
        if patched is None:
            logger.error("force_java_canonical: rebuild fail xref=%s", xref)
            return None
        out = bytearray(patched)
        changed = True
    return bytes(out) if changed or out else pdf


def break_quartz_oracle_image_cross(pdf: bytes) -> Optional[bytes]:
    """Quartz producer + Oracle-zlib-identical images → ALFA_MIXED_SERIALIZER.

    Recompress image Flate streams at a zlib level that is not an Oracle
    atlas hit, so claimed emitter stays quartz without cross_emitter images.
    """
    import fitz
    from alfa_font_extend import _find_stream_pos_for_xref, _patch_length_and_rebuild

    prod = ""
    try:
        prod = (fitz.open(stream=pdf, filetype="pdf").metadata or {}).get("producer") or ""
    except Exception:
        pass
    if "quartz" not in prod.lower() and "ios version" not in prod.lower():
        return pdf

    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception:
        return None
    image_xrefs: list[int] = []
    for xref in range(1, doc.xref_length()):
        try:
            if not doc.xref_is_stream(xref):
                continue
            obj = doc.xref_object(xref) or ""
            if "/FlateDecode" not in obj and "/Flate" not in obj:
                continue
            compact = obj.replace(" ", "")
            if "/Subtype/Image" in compact or (
                "/Width" in obj and "/Height" in obj and "/ColorSpace" in obj
            ):
                image_xrefs.append(xref)
        except Exception:
            continue
    doc.close()

    def _oracle_atlas_hit(dec: bytes, raw: bytes) -> bool:
        # Oracle image atlas defaults to zlib level 6 (see alfa_v2.streams).
        for lvl in (6, 5, 7):
            if zlib.compress(dec, lvl) == raw:
                return True
        return False

    out = bytearray(pdf)
    for xref in image_xrefs:
        try:
            d = fitz.open(stream=bytes(out), filetype="pdf")
            dec = d.xref_stream(xref)
            raw = d.xref_stream_raw(xref) or b""
            d.close()
        except Exception:
            return None
        if not dec or not _oracle_atlas_hit(dec, raw):
            continue
        new_raw = None
        for lvl in (9, 8, 4, 3, 2, 1):
            cand = zlib.compress(dec, lvl)
            if cand != raw and not _oracle_atlas_hit(dec, cand):
                new_raw = cand
                break
        if new_raw is None or new_raw == raw:
            continue
        pos = _find_stream_pos_for_xref(bytes(out), xref)
        if not pos:
            return None
        patched = _patch_length_and_rebuild(bytearray(out), pos[0], pos[1], new_raw)
        if patched is None:
            return None
        out = bytearray(patched)
        logger.info(
            "Alfa phone: re-flate image xref=%s %d→%d (break Oracle cross)",
            xref, len(raw), len(new_raw),
        )
    return bytes(out)


def _flate_fit_size(stream_b: bytes, target: int, level: int) -> Optional[bytes]:
    for lvl in (level, 6, 5, 7, 4, 8, 3, 2, 1, 9):
        comp = _oracle_near_flate(stream_b, lvl)
        if comp and len(comp) == target:
            return comp
    return None


def _nudge_quartz_exact_flate(
    stream: bytes,
    target_size: int,
    level: int = 6,
) -> Optional[bytes]:
    """Exact Deflater size for Quartz phone.

    Порядок (OnlyPDF-safe → сильнее):
      1) Jasper spaces/%x
      2) пробелы / %x перед ET
      3) детерминированный incompressible %-pad (нужен когда grow
         уменьшил compressed size; random hex/secrets — нет)
    Pad после ET запрещён.
    """
    primary = _oracle_near_flate(stream, level)
    try:
        if len(primary) == target_size and zlib.decompress(primary) == stream:
            return stream
    except zlib.error:
        pass

    try:
        from alfa_orig_mode import _nudge_stream_exact_flate

        hit = _nudge_stream_exact_flate(stream, target_size, level)
        if hit is not None:
            return hit
    except Exception:
        pass

    ets = [m.start() for m in re.finditer(rb"(?<![A-Za-z0-9])ET(?![A-Za-z0-9])", stream)]
    insert_pts = list(dict.fromkeys(ets[-12:] if ets else []))
    if not insert_pts:
        insert_pts = [len(stream)]

    def _try(cand: bytes) -> Optional[bytes]:
        if re.search(rb"ET\n[ \t%]{4,}", cand):
            return None
        comp = _oracle_near_flate(cand, level)
        if len(comp) != target_size:
            return None
        try:
            if zlib.decompress(comp) == cand:
                return cand
        except zlib.error:
            return None
        return None

    def _fill_soft(n: int, kind: str) -> bytes:
        if n <= 0:
            return b""
        if kind == "spaces":
            return b" " * n
        return b"\n%" + (b"x" * n) + b"\n"

    def _fill_hard(n: int) -> bytes:
        """Incompressible but deterministic (не secrets) — для grow under-size."""
        if n <= 0:
            return b""
        body = bytes((i * 37 + 19) % 256 for i in range(n))
        return b"\n%" + body.hex().encode("ascii") + b"\n"

    for pt in reversed(insert_pts):
        for kind in ("spaces", "comment"):
            lo, hi = 0, 400
            while lo <= hi:
                mid = (lo + hi) // 2
                fill = _fill_soft(mid, kind)
                cand = stream[:pt] + fill + stream[pt:]
                clen = len(_oracle_near_flate(cand, level))
                if clen == target_size:
                    hit = _try(cand)
                    if hit is not None:
                        return hit
                    break
                if clen < target_size:
                    lo = mid + 1
                else:
                    hi = mid - 1
            for n in range(max(0, lo - 16), min(400, lo + 32)):
                hit = _try(stream[:pt] + _fill_soft(n, kind) + stream[pt:])
                if hit is not None:
                    return hit

    # Hard pad: только если soft не довёл compressed size (типично после grow).
    for pt in reversed(insert_pts):
        lo, hi = 0, 320
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = stream[:pt] + _fill_hard(mid) + stream[pt:]
            clen = len(_oracle_near_flate(cand, level))
            if clen == target_size:
                hit = _try(cand)
                if hit is not None:
                    logger.info("Alfa phone nudge: hard-pad n=%d (grow flate fit)", mid)
                    return hit
                break
            if clen < target_size:
                lo = mid + 1
            else:
                hi = mid - 1
        for n in range(max(0, lo - 12), min(320, lo + 24)):
            hit = _try(stream[:pt] + _fill_hard(n) + stream[pt:])
            if hit is not None:
                logger.info("Alfa phone nudge: hard-pad n=%d (grow flate fit)", n)
                return hit
    return None


class AlfaPhoneOrigContext:
    """iOS Quartz Alfa phone receipt — /G1 Identity-H, scaled Tm, Tj/TJ."""

    def __init__(self) -> None:
        self.pdf_bytes: bytes = b""
        self.cid_to_uni: Dict[int, int] = {}
        self.uni_to_cid: Dict[int, int] = {}
        self.widths: Dict[int, int] = {}
        self.cs_xref = 0
        self.cs_cs = 0
        self.cs_ce = 0
        self.cs_raw = b""
        self.stream = bytearray()
        self.orig_dec_len = 0
        self.orig_comp_len = 0
        self.zlib_level = 6
        self.tu_xref = 0
        self.cid_xref = 0
        self.ff2_xref = 0

    def load(self, path: str) -> bool:
        try:
            with open(path, "rb") as f:
                return self.load_bytes(f.read())
        except Exception as exc:
            logger.warning("AlfaPhoneOrigContext.load failed: %s", exc)
            return False

    def load_bytes(self, pdf_bytes: bytes) -> bool:
        try:
            self.pdf_bytes = pdf_bytes
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            tu_xref = cid_xref = ff2_xref = None
            for xref in range(1, doc.xref_length()):
                obj = doc.xref_object(xref)
                if "/Subtype /Type0" in obj and "/ToUnicode" in obj:
                    tu_xref = int(re.search(r"/ToUnicode (\d+)", obj).group(1))
                    m_cid = re.search(r"/DescendantFonts\s*\[\s*(\d+)", obj)
                    if m_cid:
                        cid_xref = int(m_cid.group(1))
                if "/FontFile2" in obj:
                    m = re.search(r"/FontFile2 (\d+)", obj)
                    if m:
                        ff2_xref = int(m.group(1))
            if not tu_xref:
                doc.close()
                return False
            tu = doc.xref_stream(tu_xref).decode("latin1", "replace")
            self.cid_to_uni = _parse_bfrange(tu)
            self.uni_to_cid = {u: c for c, u in self.cid_to_uni.items()}
            self.tu_xref = tu_xref or 0
            self.cid_xref = cid_xref or 0
            self.ff2_xref = ff2_xref or 0
            page = doc[0]
            cs_xref = page.get_contents()[0]
            try:
                dec = doc.xref_stream(cs_xref)
            except Exception:
                doc.close()
                return False
            if not dec or b"Tm" not in dec or b"<" not in dec:
                doc.close()
                return False
            self.cs_xref = cs_xref
            doc.close()
            sm = _find_stream_pos_for_xref(self.pdf_bytes, cs_xref)
            if sm is None:
                return False
            self.cs_cs, self.cs_ce = sm
            self.cs_raw = self.pdf_bytes[self.cs_cs : self.cs_ce]
            self.orig_comp_len = len(self.cs_raw)
            self.zlib_level = _zlib_level_from_header(self.cs_raw)
            self.stream = bytearray(dec)
            self.orig_dec_len = len(self.stream)
            return True
        except Exception as exc:
            logger.warning("AlfaPhoneOrigContext.load_bytes failed: %s", exc)
            return False

    def dec(self, hex_ascii: bytes) -> str:
        h = hex_ascii.decode("ascii")
        return "".join(
            chr(self.cid_to_uni.get(int(h[i : i + 4], 16), 0x3F))
            for i in range(0, len(h), 4)
        )

    def enc(self, text: str) -> bytes:
        out = []
        for ch in text:
            cp = 0x00A0 if ch in (" ", _NBSP) else ord(ch)
            cid = self.uni_to_cid.get(cp)
            if cid is None:
                logger.error("Alfa phone enc: missing CID for %r (U+%04X)", ch, cp)
                return b""
            out.append(f"{cid:04X}")
        return "".join(out).encode("ascii")

    def can_render(self, text: str) -> Tuple[bool, List[str]]:
        miss = []
        for ch in text:
            if ch in ("\n", "\r", "\t"):
                continue
            cp = 0x00A0 if ch == " " else ord(ch)
            if cp not in self.uni_to_cid:
                miss.append(ch)
        return (len(miss) == 0, miss)

    def _text_op_at(self, y: float, x: float) -> Optional[Tuple[int, int, str, str]]:
        """Return (op_start, op_end, kind, hex_or_full) for Tj/TJ near (x,y)."""
        best = None
        best_dist = 1e9
        for m in _TM_RE.finditer(bytes(self.stream)):
            mx, my = float(m.group(3)), float(m.group(4))
            if abs(my - y) > 1.5 or abs(mx - x) > 3.0:
                continue
            tail_off = m.end()
            tail = bytes(self.stream)[tail_off : tail_off + 600]
            tj = re.search(rb"<([0-9A-Fa-f]+)>\s*Tj", tail)
            tj_arr = re.search(rb"\[(?:[^\[\]]|\n)*\]\s*TJ", tail, re.S)
            cand = None
            if tj and (not tj_arr or tj.start() < tj_arr.start()):
                op_s = tail_off + tj.start()
                op_e = tail_off + tj.end()
                cand = (op_s, op_e, "Tj", tj.group(1).decode("ascii"))
            elif tj_arr:
                op_s = tail_off + tj_arr.start()
                op_e = tail_off + tj_arr.end()
                parts = re.findall(rb"<([0-9A-Fa-f]+)>", tj_arr.group(0))
                hx = b"".join(parts).decode("ascii")
                cand = (op_s, op_e, "TJ", hx)
            if not cand:
                continue
            dist = abs(my - y) + abs(mx - x) * 0.01
            if dist < best_dist:
                best_dist = dist
                best = cand
        return best

    def slot_size_at(self, y: float, x: float) -> int:
        op = self._text_op_at(y, x)
        if not op:
            return 0
        return len(op[3]) // 4

    def extract_at(self, y: float, x: float) -> str:
        op = self._text_op_at(y, x)
        if not op:
            return ""
        return self.dec(op[3].encode("ascii"))

    def replace_at(self, y: float, x: float, new_text: str) -> bool:
        op = self._text_op_at(y, x)
        if not op:
            return False
        op_s, op_e, kind, old_hx = op
        # Pad a shorter exact payload, but never truncate a longer one.
        # After «RUR» use U+0020, not NBSP — Proton ALFA_AMOUNT_TYPOGRAPHY_ANOMALY.
        t = new_text
        n_old = len(old_hx) // 4
        if len(t.rstrip(_NBSP)) > n_old:
            logger.warning(
                "Alfa phone replace too long: need %d have %d",
                len(t.rstrip(_NBSP)),
                n_old,
            )
            return False
        pad_ch = " " if "RUR" in t.replace("\xa0", " ") else _NBSP
        while len(t) < n_old:
            t += pad_ch
        hx = self.enc(t)
        if not hx:
            return False
        # always write as <hex> Tj (simpler, Quartz already mixes)
        new_op = b"<" + hx + b"> Tj"
        self.stream[op_s:op_e] = new_op
        return True

    def grow_slot_at(self, y: float, x: float, need: int) -> bool:
        """Expand hex slot to `need` CIDs (pad with NBSP CID)."""
        op = self._text_op_at(y, x)
        if not op:
            return False
        op_s, op_e, kind, old_hx = op
        n_old = len(old_hx) // 4
        if need <= n_old:
            return True
        text = self.dec(old_hx.encode("ascii"))
        text = text + (_NBSP * (need - len(text)))
        hx = self.enc(text[:need])
        if not hx or len(hx) // 4 != need:
            return False
        new_op = b"<" + hx + b"> Tj"
        self.stream[op_s:op_e] = new_op
        return True

    def fits_fields(self, coords: Dict[str, Tuple[float, float]], prepared: Dict[str, str]) -> Tuple[bool, str]:
        for key, (y, x) in coords.items():
            if key not in prepared:
                continue
            need = len(prepared[key].rstrip(_NBSP))
            have = self.slot_size_at(y, x)
            if have <= 0:
                return False, f"no slot {key}"
            if need > have:
                return False, f"{key} need {need} have {have}"
            ok, miss = self.can_render(prepared[key])
            if not ok:
                return False, f"{key} missing {miss}"
        return True, ""

    def commit(self) -> Optional[bytes]:
        """Exact flate = donor /Length; Quartz entropy-nudge; never Length/xref rebuild."""
        stream_b = bytes(self.stream)
        compressed = _flate_fit_size(stream_b, self.orig_comp_len, self.zlib_level)
        if compressed is None or len(compressed) != self.orig_comp_len:
            nudged = _nudge_quartz_exact_flate(
                stream_b, self.orig_comp_len, self.zlib_level,
            )
            if nudged is not None:
                self.stream = bytearray(nudged)
                stream_b = nudged
                compressed = _oracle_near_flate(stream_b, self.zlib_level)

        out = bytearray(self.pdf_bytes)
        if compressed is not None and len(compressed) == self.orig_comp_len:
            out[self.cs_cs : self.cs_ce] = compressed
        else:
            # SafeCheck «структура» + Proton rebuild fingerprints: never Length/xref.
            logger.warning(
                "Alfa phone: flate size miss need=%d got=%s — reject (no rebuild)",
                self.orig_comp_len,
                None if compressed is None else len(compressed),
            )
            return None
        from sber_dynamic import _strip_pdf_eof_tail

        return _strip_pdf_eof_tail(bytes(out))

    @property
    def available_chars(self) -> set:
        return {chr(u) for u in self.uni_to_cid if u >= 32}
