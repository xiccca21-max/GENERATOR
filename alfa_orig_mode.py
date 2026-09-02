"""Alfa orig-mode: Tahoma Identity-H, concatenated <CIDhex> Tj blocks."""
from __future__ import annotations

import re
import zlib
import logging
from typing import Dict, List, Optional, Tuple

import fitz

logger = logging.getLogger(__name__)

_NBSP = "\u00a0"
# Quartz phone often splits `x y` / `Tm` across a newline.
_TM_RE = re.compile(
    rb"(?:1 0 0 1|[\d.]+ 0 0 [\d.]+) ([\d.]+) ([\d.]+)\s+Tm"
)
# Semantic trailing NBSP (CID 000A): operation id, FIO, date_time (with seconds).
# date_formed is minute-only «DD.MM.YYYY HH:MM мск» — no trailing NBSP (donor slot).
# phone / bank / account / SBP / message — no trailing NBSP.
_TRAILING_NBSP_KEYS = frozenset({
    "receiver", "operation_num", "date_time",
})


def _need_len(text: str, *, key: str = "") -> int:
    """Visible slot length. Keep one trailing NBSP on RUR / FIO / op number.

    Do not treat every field that happens to end with NBSP as +1 — that grew
    date_time slots and made content flate miss donor /Length (1001).
    """
    stripped = (text or "").rstrip(_NBSP)
    if stripped.endswith("RUR") or key in _TRAILING_NBSP_KEYS:
        return len(stripped) + 1
    return len(stripped)


def _slot_face_text(new_text: str, *, key: str = "") -> str:
    """Keep trailing NBSP only on RUR / FIO / operation_num.

    date_time formatter may add a spare NBSP after «мск» — 03 did not paint it.
    """
    t = (new_text or "").rstrip(_NBSP)
    if t.endswith("RUR") or key in _TRAILING_NBSP_KEYS:
        t = t + _NBSP
    return t


def _parse_bfchar(tu: str) -> Dict[int, int]:
    """ToUnicode → {cid: codepoint}. Oracle bfchar + Quartz bfrange."""
    out: Dict[int, int] = {}
    for block in re.finditer(r"\d+\s+beginbfrange(.*?)endbfrange", tu, re.S | re.I):
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
    for block in re.finditer(r"beginbfchar(.*?)endbfchar", tu, re.S | re.I):
        for m in re.finditer(r"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]{4})>", block.group(1)):
            out[int(m.group(1), 16)] = int(m.group(2), 16)
    return out


def _extract_w_array_body(obj: str) -> str:
    m = re.search(r"/W\s*\[", obj, re.S)
    if not m:
        return ""
    i = m.end() - 1
    depth = 0
    while i < len(obj):
        if obj[i] == "[":
            depth += 1
        elif obj[i] == "]":
            depth -= 1
            if depth == 0:
                return obj[m.end() : i]
        i += 1
    return ""


def _parse_widths(obj: str) -> Dict[int, int]:
    body = _extract_w_array_body(obj)
    if not body:
        return {}
    widths: Dict[int, int] = {}
    tokens = re.findall(r"-?\d+\.?\d*|\[|\]", body)
    j = 0
    while j < len(tokens):
        if tokens[j] in ("[", "]"):
            j += 1
            continue
        try:
            gid = int(float(tokens[j]))
        except ValueError:
            j += 1
            continue
        j += 1
        if j < len(tokens) and tokens[j] == "[":
            j += 1
            run: List[int] = []
            while j < len(tokens) and tokens[j] != "]":
                try:
                    run.append(int(float(tokens[j])))
                except ValueError:
                    pass
                j += 1
            if j < len(tokens) and tokens[j] == "]":
                j += 1
            for k, w in enumerate(run):
                widths[gid + k] = w
        elif j + 1 < len(tokens):
            try:
                gid_hi = int(float(tokens[j]))
                w = int(float(tokens[j + 1]))
                j += 2
                for g in range(gid, gid_hi + 1):
                    widths[g] = w
            except ValueError:
                j += 1
    return widths


def _serialize_w_compact(widths: Dict[int, int]) -> bytes:
    """Serialize /W mapping as grouped consecutive CID runs."""
    if not widths:
        return b"/W []"
    parts: list[bytes] = [b"/W ["]
    gids = sorted(widths)
    i = 0
    while i < len(gids):
        j = i
        while j + 1 < len(gids) and gids[j + 1] == gids[j] + 1:
            j += 1
        run_gids = gids[i : j + 1]
        run_widths = [widths[g] for g in run_gids]
        # /W syntax: <firstCID> [<w1> <w2> ...]
        parts.append(
            f"{run_gids[0]}[".encode("ascii")
            + b" ".join(str(w).encode("ascii") for w in run_widths)
            + b"]"
        )
        i = j + 1
    parts.append(b"]")
    return b"".join(parts)


def _compact_font_w_arrays(pdf: bytes) -> bytes:
    """Убирает pretty-printed /W (пробелы, CRLF) без потери записей."""
    out = bytearray(pdf)
    for m in list(re.finditer(rb"/W\s*\[", out)):
        start = m.start()
        depth = 0
        end = None
        i = m.end() - 1
        while i < len(out):
            if out[i : i + 1] == b"[":
                depth += 1
            elif out[i : i + 1] == b"]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1
        if end is None:
            continue
        chunk = bytes(out[start:end])
        try:
            # Parse /W body but keep the numeric width lexemes verbatim.
            # Fitz is sensitive to exact width tokens; converting them to int
            # can lead to missing/garbled characters in some SBP fields.
            obj_str = chunk.decode("latin1", "replace")
            m2 = re.search(r"/W\s*\[([^\]]*)\]", obj_str, re.S)
            if not m2:
                continue
            body = m2.group(1)
            tokens = re.findall(r"-?\d+(?:\.\d+)?|\[|\]", body)
            if not tokens:
                continue

            widths: Dict[int, str] = {}
            j = 0
            while j < len(tokens):
                if tokens[j] in ("[", "]"):
                    j += 1
                    continue
                try:
                    gid = int(float(tokens[j]))
                except ValueError:
                    j += 1
                    continue
                j += 1
                if j < len(tokens) and tokens[j] == "[":
                    j += 1
                    run: list[str] = []
                    while j < len(tokens) and tokens[j] != "]":
                        run.append(tokens[j])
                        j += 1
                    if j < len(tokens) and tokens[j] == "]":
                        j += 1
                    for k, w_tok in enumerate(run):
                        widths[gid + k] = w_tok
                elif j + 1 < len(tokens):
                    try:
                        gid_hi = int(float(tokens[j]))
                        w_tok = tokens[j + 1]
                        j += 2
                        for g in range(gid, gid_hi + 1):
                            widths[g] = w_tok
                    except ValueError:
                        j += 1

            compact = _serialize_w_compact(widths)  # supports str widths
        except Exception:
            continue
        if len(compact) > len(chunk):
            continue
        out[start:end] = compact + b" " * (len(chunk) - len(compact))
    return bytes(out)


def _find_stream_pos_for_xref(pdf: bytes, xref: int) -> Optional[Tuple[int, int]]:
    needle = f"\n{xref} 0 obj".encode()
    pos = pdf.find(needle)
    if pos < 0:
        pos = pdf.find(f"{xref} 0 obj".encode())
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
    ce = es
    if pdf[ce - 2 : ce] == b"\r\n":
        ce -= 2
    elif pdf[ce - 1 : ce] == b"\n":
        ce -= 1
    return cs, ce


def _zlib_level_from_header(header: bytes) -> int:
    if header[:2] == b"\x78\x01":
        return 1
    if header[:2] == b"\x78\x5e":
        return 5
    if header[:2] == b"\x78\x9c":
        return 6
    if header[:2] == b"\x78\xda":
        return 9
    return 6


def _pad_to_compressed_size(stream: bytes, target_size: int, level: int) -> Optional[bytes]:
    """Сжатие до target_size без padding в decoded stream (валидатор ловит % после ET)."""
    for lvl in (level, 6, 5, 7, 4, 8, 3, 2, 1, 9):
        comp = zlib.compress(stream, lvl)
        if len(comp) == target_size:
            return comp
    return None


def _oracle_near_flate(data: bytes, level: int = 6) -> bytes:
    """Сжатие content как у Oracle BI: Java Deflater(level, false).

    PROTON: 5/6 Java-canonical + 1/6 foreign → ALFA_MIXED_ZLIB.
    Нужно 6/6 Java Deflater(6). Fallback — CPython zlib (хуже для PROTON).
    """
    from alfa_java_deflate import java_deflate

    lvl = level if 1 <= level <= 9 else 6
    out = java_deflate(data, level=lvl)
    if out is not None:
        try:
            if zlib.decompress(out) == data:
                return out
        except zlib.error:
            pass
        logger.warning("Alfa java_deflate roundtrip failed; fallback zlib")
    return zlib.compress(data, lvl)


def _unreproducible_flate(data: bytes, level: int = 6) -> bytes:
    """Совместимость: то же, что Oracle Java Deflater."""
    return _oracle_near_flate(data, level)


def _nudge_stream_exact_flate(
    stream: bytes,
    target_size: int,
    level: int = 6,
) -> Optional[bytes]:
    """Подгонка decoded content так, чтобы Java Deflater(level) дал ровно target_size.

    Alfa / Jasper content часто с ``\\r\\n``; pad только ``x`` почти не растёт
    compressed size — нужны CRLF-точки вставки + низкосжимаемый %-comment.
    """
    primary = _oracle_near_flate(stream, level)
    try:
        if len(primary) == target_size and zlib.decompress(primary) == stream:
            return stream
    except zlib.error:
        pass

    nl = b"\r\n" if stream.count(b"\r\n") >= stream.count(b"\n") else b"\n"

    def _noise(n: int) -> bytes:
        # Printable ASCII, low zlib ratio (not a run of identical bytes).
        return bytes(33 + ((i * 37 + 11) % 94) for i in range(max(0, n)))

    insert_pts: list[int] = []
    # Before ET of painted text blocks (LF and CRLF).
    for m in re.finditer(rb"0 g\r?\n(?=ET)", stream):
        insert_pts.append(m.end())
    for m in re.finditer(rb"\r?\n(?=ET\b)", stream):
        insert_pts.append(m.start() + (2 if stream[m.start() : m.start() + 2] == b"\r\n" else 1))
    for m in re.finditer(rb"(?:\r?\n|\s)(?:ET|Q|q|BT)\b", stream):
        insert_pts.append(m.start() + 1)
    # Empty trailing text object: insert before final ET.
    for m in re.finditer(rb"/F1 \d+ Tf\r?\n(?=ET)", stream):
        insert_pts.append(m.end())
    insert_pts.append(len(stream))

    # Drop points that sit after an ET with no newer BT (post-text zone).
    safe_pts: list[int] = []
    for pt in insert_pts:
        if pt < 0 or pt > len(stream):
            continue
        if pt < len(stream) and stream[pt : pt + 7] == b"1 0 0 1":
            continue
        prefix = stream[:pt]
        last_et = prefix.rfind(b"ET")
        last_bt = prefix.rfind(b"BT")
        if last_et >= 0 and last_et > last_bt and pt < len(stream):
            continue
        if pt not in safe_pts:
            safe_pts.append(pt)
    if not safe_pts:
        safe_pts = [len(stream)]

    def _try(cand: bytes) -> Optional[bytes]:
        # Reject OnlyPDF-burned pad-after-ET shape when applicable.
        if re.search(rb"0 g\r?\nET\r?\n[ \t%]{4,}", cand):
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

    def _fill(kind: str, n: int) -> bytes:
        if n <= 0:
            return b""
        if kind == "noise":
            return nl + b"%" + _noise(n) + nl
        if kind == "comment":
            return nl + b"%" + (b"x" * n) + nl
        return b" " * n

    # 1) Binary search fill on safe points (noise first — reaches +40..+100 flate).
    for pt in safe_pts[:8]:
        for fill_kind in ("noise", "comment", "spaces"):
            lo, hi = 0, 2048 if fill_kind == "noise" else 1024
            best_hit: Optional[bytes] = None
            while lo <= hi:
                mid = (lo + hi) // 2
                fill = _fill(fill_kind, mid)
                cand = stream + fill if pt >= len(stream) else stream[:pt] + fill + stream[pt:]
                clen = len(_oracle_near_flate(cand, level))
                if clen == target_size:
                    hit = _try(cand)
                    if hit is not None:
                        return hit
                    best_hit = cand
                    break
                if clen < target_size:
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best_hit is not None:
                hit = _try(best_hit)
                if hit is not None:
                    return hit
            # Fine scan around BS boundary (zlib size can plateau / jump).
            # After loop: hi = last undersize, lo = first oversize (lo > hi).
            for n in range(max(0, hi - 2), min(max(lo, hi) + 32, 2048) + 1):
                fill = _fill(fill_kind, n)
                cand = stream + fill if pt >= len(stream) else stream[:pt] + fill + stream[pt:]
                hit = _try(cand)
                if hit is not None:
                    return hit

    # 2) Exhaustive small fillers.
    fillers = [
        nl, b" ", b"  ", nl + nl, nl + b" " + nl,
        nl + b"%" + nl, nl + b"%." + nl, nl + b"%.." + nl,
        nl + b"%..." + nl, nl + b"%...." + nl,
        nl + b"%a" + nl, nl + b"%b" + nl, nl + b"%c" + nl,
    ]
    for n in range(1, 96):
        fillers.append(_fill("noise", n))
        fillers.append(_fill("comment", n))
        fillers.append(b" " * n)
    seen: set[bytes] = set()
    for pt in safe_pts[:6]:
        for fill in fillers:
            cand = stream + fill if pt >= len(stream) else stream[:pt] + fill + stream[pt:]
            if cand in seen:
                continue
            seen.add(cand)
            hit = _try(cand)
            if hit is not None:
                return hit
    return None


def _flate_fit_size(data: bytes, target_size: int, level: int = 6) -> Optional[bytes]:
    """Подогнать Flate к точному target_size без xref-rebuild.

    Oracle level=6: только точный Java Deflater(6) без \\x00-хвоста
    (хвост → ALFA_MIXED_ZLIB: content foreign при canonical image/font).
    """
    primary = _oracle_near_flate(data, level)
    try:
        if len(primary) == target_size and zlib.decompress(primary) == data:
            return primary
    except zlib.error:
        pass
    # Для Oracle BI content pad запрещён.
    if level == 6:
        return None
    if len(primary) < target_size:
        need = target_size - len(primary)
        if 1 <= need <= 64:
            out = primary + (b"\x00" * need)
            try:
                if zlib.decompress(out) == data and len(out) == target_size:
                    return out
            except zlib.error:
                pass
    levels = []
    for lvl in (level, 6, 5, 7, 4, 8, 3, 2, 1, 9):
        if lvl not in levels:
            levels.append(lvl)
    best: Optional[bytes] = None
    for lvl in levels:
        comp = _oracle_near_flate(data, lvl)
        if len(comp) == target_size:
            try:
                if zlib.decompress(comp) == data:
                    return comp
            except zlib.error:
                pass
        if len(comp) < target_size:
            if best is None or len(comp) > len(best):
                best = comp
    if best is not None:
        need = target_size - len(best)
        if 1 <= need <= 64:
            out = best + (b"\x00" * need)
            try:
                if zlib.decompress(out) == data and len(out) == target_size:
                    return out
            except zlib.error:
                pass
    return None


def homogenize_oracle_flate(pdf: bytes, level: int = 6) -> Optional[bytes]:
    """НЕ использовать для выдачи: ломает распознавание (image/font fingerprint).

    Оставлено как отладочный хелпер.
    """
    return pdf  # no-op — recognition requires corpus-canonical asset bytes


class AlfaOrigContext:
    def __init__(self) -> None:
        self.pdf_bytes: bytes = b""
        self.cid_to_uni: Dict[int, int] = {}
        self.uni_to_cid: Dict[int, int] = {}
        self.widths: Dict[int, int] = {}
        self.upem = 1000
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
                raw = f.read()
            return self.load_bytes(raw)
        except Exception as exc:
            logger.warning("AlfaOrigContext.load failed: %s", exc)
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
                    cid_xref = int(re.search(r"/DescendantFonts\s*\[\s*(\d+)", obj).group(1))
                if "/FontFile2" in obj:
                    m = re.search(r"/FontFile2 (\d+)", obj)
                    if m:
                        ff2_xref = int(m.group(1))
            if not tu_xref:
                doc.close()
                return False
            tu = doc.xref_stream(tu_xref).decode("latin1", "replace")
            self.cid_to_uni = _parse_bfchar(tu)
            self.uni_to_cid = {u: c for c, u in self.cid_to_uni.items()}
            self.tu_xref = tu_xref or 0
            self.cid_xref = cid_xref or 0
            self.ff2_xref = ff2_xref or 0
            if cid_xref:
                self.widths = _parse_widths(doc.xref_object(cid_xref))
            page = doc[0]
            cs_xref = page.get_contents()[0]
            try:
                dec = doc.xref_stream(cs_xref)
            except Exception:
                doc.close()
                return False
            if not dec or b"1 0 0 1" not in dec or b"<" not in dec:
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
            logger.warning("AlfaOrigContext.load failed: %s", exc)
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
                logger.error("Alfa enc: missing CID for %r (U+%04X)", ch, cp)
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

    def _slot_at(self, y: float, x: float) -> Optional[Tuple[int, int, int, str]]:
        best: Optional[Tuple[int, int, int, str]] = None
        for m in _TM_RE.finditer(bytes(self.stream)):
            mx, my = float(m.group(1)), float(m.group(2))
            if abs(my - y) > 1.5 or abs(mx - x) > 3.0:
                continue
            tail = bytes(self.stream)[m.end() : m.end() + 220]
            tj = re.search(rb"<([0-9A-Fa-f]+)>", tail)
            if not tj or len(tj.group(1)) < 8:
                continue
            hex_b = tj.group(1)
            n = len(hex_b) // 4
            abs_start = m.end() + tj.start(1)
            abs_end = m.end() + tj.end(1)
            old_t = self.dec(hex_b)
            if best is None or n > best[2]:
                best = (abs_start, abs_end, n, old_t)
        return best

    def slot_size_at(self, y: float, x: float) -> int:
        row = self._slot_at(y, x)
        return row[2] if row else 0

    def fits_text_at(self, y: float, x: float, new_text: str, *, key: str = "") -> bool:
        row = self._slot_at(y, x)
        if not row:
            return False
        _, _, slot_chars, _ = row
        t = _slot_face_text(new_text, key=key)
        if len(t) > slot_chars:
            return False
        if len(t) < slot_chars:
            t = t + _NBSP * (slot_chars - len(t))
        return self.can_render(t)[0]

    def fits_fields(self, coords: Dict[str, Tuple[float, float]], prepared: Dict[str, str]) -> Tuple[bool, str]:
        from alfa_font_extend import _glyphs_ok_for_text

        for key, (y, x) in coords.items():
            if key not in prepared:
                continue
            if not self.fits_text_at(y, x, prepared[key], key=key):
                need = _need_len(prepared[key], key=key)
                have = self.slot_size_at(y, x)
                return False, f"{key}:{need}>{have}"
        body = "".join(prepared.get(k, "") for k in coords)
        ok, miss = _glyphs_ok_for_text(self, self.pdf_bytes, body)
        if not ok:
            return False, f"glyph:{''.join(miss[:5])}"
        return True, ""

    def replace_at(self, y: float, x: float, new_text: str, *, key: str = "") -> bool:
        row = self._slot_at(y, x)
        if not row:
            return False
        start, end, slot_chars, _old_t = row
        nbsp = _NBSP
        t = _slot_face_text(new_text, key=key)
        if len(t) > slot_chars:
            logger.warning(
                "Alfa replace too long at y=%s: need %d have %d (%s)",
                y, len(t), slot_chars, new_text[:30],
            )
            return False
        if len(t) < slot_chars:
            t = t + nbsp * (slot_chars - len(t))
        ok, miss = self.can_render(t)
        if not ok:
            logger.warning("Alfa replace miss %s at y=%s: %s", miss, y, new_text[:30])
            return False
        from alfa_font_extend import _glyphs_ok_for_text

        ok_g, miss_g = _glyphs_ok_for_text(self, self.pdf_bytes, t)
        if not ok_g:
            logger.warning("Alfa replace bad glyf %s at y=%s: %s", miss_g, y, new_text[:30])
            return False
        new_hex = self.enc(t)
        if not new_hex or len(new_hex) != (end - start):
            return False
        self.stream[start:end] = new_hex
        return True

    def extract_at(self, y: float, x: float) -> str:
        row = self._slot_at(y, x)
        return row[3] if row else ""

    def grow_slot_at(self, y: float, x: float, need_chars: int) -> bool:
        """Увеличить hex-слот до need_chars (паддинг NBSP) — без правки FontFile2."""
        row = self._slot_at(y, x)
        if not row:
            return False
        start, end, slot_chars, _old = row
        if need_chars <= slot_chars:
            return True
        pad_cp = 0x00A0 if 0x00A0 in self.uni_to_cid else (0x20 if 0x20 in self.uni_to_cid else None)
        if pad_cp is None:
            return False
        pad_cid = self.uni_to_cid[pad_cp]
        pad_n = need_chars - slot_chars
        pad_hex = f"{pad_cid:04X}".encode("ascii") * pad_n
        self.stream[end:end] = pad_hex
        return True

    def shrink_slot_at(self, y: float, x: float, new_chars: int) -> bool:
        """Укоротить hex-слот до new_chars (хвост — пад). Декодированная длина CS падает."""
        row = self._slot_at(y, x)
        if not row:
            return False
        start, end, slot_chars, _old = row
        if new_chars == slot_chars:
            return True
        if new_chars < 1 or new_chars > slot_chars:
            return False
        new_end = start + new_chars * 4
        if new_end > end or (end - new_end) % 4 != 0:
            return False
        del self.stream[new_end:end]
        return True

    def rebalance_slots(
        self,
        coords: Dict[str, Tuple[float, float]],
        prepared: Dict[str, str],
        *,
        extra_need: Optional[Dict[str, int]] = None,
    ) -> bool:
        """Grow short fields; steal trailing hex from long ones so decoded CS stays put."""
        extra = extra_need or {}
        grows: List[Tuple[str, float, float, int]] = []
        surplus: List[Tuple[str, float, float, int, int]] = []
        for key, (y, x) in coords.items():
            if key not in prepared:
                continue
            need = _need_len(prepared[key], key=key) + int(extra.get(key, 0))
            have = self.slot_size_at(y, x)
            if have <= 0:
                continue
            if need > have:
                grows.append((key, y, x, need))
            elif have > need:
                surplus.append((key, y, x, have, need))
        total_grow = 0
        for _key, y, x, need in grows:
            have = self.slot_size_at(y, x)
            total_grow += max(0, need - have)
        stolen = 0
        surplus.sort(key=lambda t: t[3] - t[4], reverse=True)
        for _key, y, x, have, need in surplus:
            if stolen >= total_grow:
                break
            take = min(have - need, total_grow - stolen)
            if take <= 0:
                continue
            if self.shrink_slot_at(y, x, have - take):
                stolen += take
        for _key, y, x, need in grows:
            if not self.grow_slot_at(y, x, need):
                return False
        return True

    def trim_spare_pad(
        self,
        coords: Dict[str, Tuple[float, float]],
        prepared: Dict[str, str],
        *,
        extra_need: Optional[Dict[str, int]] = None,
    ) -> bool:
        """Drop one trailing pad CID from the longest spare slot (flate overshoot)."""
        extra = extra_need or {}
        best = None
        best_spare = 0
        for key, (y, x) in coords.items():
            if key not in prepared:
                continue
            need = _need_len(prepared[key], key=key) + int(extra.get(key, 0))
            have = self.slot_size_at(y, x)
            spare = have - need
            if spare > best_spare:
                best_spare = spare
                best = (y, x, have)
        if not best:
            return False
        y, x, have = best
        return self.shrink_slot_at(y, x, have - 1)

    def ensure_slot_at(self, y: float, x: float, text: str) -> bool:
        need = _need_len(text)
        if need <= 0:
            return True
        if self.slot_size_at(y, x) >= need:
            return True
        return self.grow_slot_at(y, x, need)

    def commit(self) -> Optional[bytes]:
        stream_b = bytes(self.stream)
        # near-oracle flate: не трогаем image/font (распознавание), content — near zlib-6.
        compressed = _flate_fit_size(stream_b, self.orig_comp_len, self.zlib_level)
        if compressed is None:
            nudged = _nudge_stream_exact_flate(
                stream_b, self.orig_comp_len, self.zlib_level,
            )
            if nudged is not None:
                self.stream[:] = bytearray(nudged)
                stream_b = bytes(self.stream)
                compressed = _flate_fit_size(
                    stream_b, self.orig_comp_len, self.zlib_level,
                )
        if compressed is None:
            # SafeCheck «структура»: never Length/xref-rebuild content stream.
            # Prefer GEN_FAIL → retry another payload/donor over structure FAKE.
            logger.warning(
                "Alfa: flate size miss need=%d dec=%d — reject (no xref rebuild)",
                self.orig_comp_len,
                len(stream_b),
            )
            return None
        out = bytearray(self.pdf_bytes)
        if len(compressed) != self.orig_comp_len:
            logger.error(
                "Alfa: flate fit returned %d≠%d — reject (no xref rebuild)",
                len(compressed), self.orig_comp_len,
            )
            return None
        out[self.cs_cs : self.cs_ce] = compressed
        from sber_dynamic import _strip_pdf_eof_tail
        return _strip_pdf_eof_tail(bytes(out))

    def commit_rebuild(self) -> Optional[bytes]:
        """Java Deflater(6,false) + /Length + xref + startxref (05_fio PASS)."""
        from tbank_sbp_stealth import _patch_length_and_rebuild
        from sber_dynamic import _strip_pdf_eof_tail

        stream_b = bytes(self.stream)
        compressed = _oracle_near_flate(stream_b, self.zlib_level)
        patched = _patch_length_and_rebuild(
            bytearray(self.pdf_bytes), self.cs_cs, self.cs_ce, compressed,
        )
        if patched is None:
            logger.warning("Alfa: xref rebuild after Deflater failed")
            return None
        return _strip_pdf_eof_tail(bytes(patched))

    @property
    def available_chars(self) -> set:
        return {chr(u) for u in self.uni_to_cid if u >= 32}


def load_available_chars(path: str) -> set:
    ctx = AlfaOrigContext()
    if ctx.load(path):
        return ctx.available_chars
    return set()


def union_available_chars(paths: List[str]) -> set:
    out: set = set()
    for p in paths:
        out |= load_available_chars(p)
    return out
