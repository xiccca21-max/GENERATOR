"""ALFA statement (выписка по счёту) from the Alfa app shell.

App orig is Oracle BI painted, then iText 4.2 PdfStamper (Producer
``modified using iText``, ``/ITXT``, signature Xi0/Xi1, unequal /ID).
Keep that shell. F2/F3 stay byte-identical. F1 hydrates only when the
donor cmap cannot paint the face.
"""
from __future__ import annotations

import logging
import os
import random
import re
import string
from copy import deepcopy
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Optional, Tuple

from time_msk import now_msk

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
STATEMENT_ORIG = os.path.join(_DIR, "templates", "Alfa_statement_original.pdf")
_ARIAL_COVER = os.path.join(_DIR, "alfa_glyph_library", "arial_cover.ttf")
_WIN_ARIAL = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf")

_NBSP = "\u00a0"
_INCOMING_ORIG = 29319.35

# PDF Tm (y, x) of F1 value slots on the donor. Labels stay put.
STATEMENT_COORDS: Dict[str, Tuple[float, float]] = {
    "account": (670.546, 146.65),
    "date_formed": (625.646, 146.65),
    "client": (601.598, 146.65),
    "address_1": (590.248, 146.65),
    "address_2": (581.049, 146.65),
    "address_3": (571.85, 146.65),
    "address_4": (562.651, 146.65),
    "period": (675.296, 311.85),
    "incoming": (662.497, 511.856),
    "expenses": (628.397, 511.856),
    "outgoing": (611.347, 511.856),
    "pay_limit": (594.297, 511.856),
    "current_balance": (537.497, 511.856),
    "op_date": (427.104, 27.25),
    "op_code": (427.104, 96.4),
    "desc_1": (427.104, 188.35),
    "desc_2": (417.905, 188.35),
    "tx_amount": (427.104, 510.242),
    "hold_1": (403.006, 188.35),
}

_RIGHT_KEYS = frozenset({
    "incoming", "expenses", "outgoing", "pay_limit", "current_balance", "tx_amount",
})
_RIGHT_EDGE = 566.97
_ADDR_MAX_W = 125.2
_DESC_MAX_W = 310.0
_PT = 8.0

_ADDR_POOL = (
    ("101000", "г. Москва", "ул. Арбат", 12, 8),
    ("190000", "г. Санкт-Петербург", "Литейный пр-кт", 34, 11),
    ("620014", "г. Екатеринбург", "ул. Ленина", 52, 19),
    ("630099", "г. Новосибирск", "Красный пр-кт", 77, 4),
    ("344002", "г. Ростов-на-Дону", "ул. Большая Садовая", 18, 22),
    ("603000", "г. Нижний Новгород", "ул. Большая Покровская", 9, 15),
    ("443099", "г. Самара", "ул. Молодогвардейская", 41, 6),
    ("420111", "г. Казань", "ул. Баумана", 25, 31),
    ("450000", "г. Уфа", "ул. Ленина", 63, 2),
    ("614000", "г. Пермь", "ул. Сибирская", 15, 44),
    ("660017", "г. Красноярск", "пр-кт Мира", 88, 9),
    ("690091", "г. Владивосток", "ул. Светланская", 27, 17),
    ("350000", "г. Краснодар", "ул. Красная", 70, 5),
    ("400066", "г. Волгоград", "пр-кт Ленина", 10, 28),
    ("394018", "г. Воронеж", "пр-кт Революции", 21, 13),
)
# Donor F1 subset has no М/Л/Г… — keep a fallback that paints without hydrate.
_ADDR_SAFE = (
    ("354000", "г. Сочи", "ул. Навагинская", 12, 8),
    ("420111", "г. Казань", "пр-кт Победы", 25, 4),
    ("450000", "г. Уфа", "ул. Октября", 63, 11),
    ("410000", "г. Саратов", "ул. Чапаева", 18, 7),
    ("440000", "г. Пенза", "ул. Кирова", 9, 22),
)


def _is_auto(value) -> bool:
    return str(value or "").strip().lower() in ("авто", "auto", "-", "")


def _parse_date(raw, *, year: Optional[int] = None) -> str:
    text = str(raw or "").strip()
    if _is_auto(text) or text.lower() in ("сейчас", "now"):
        return now_msk().strftime("%d.%m.%Y")
    text = text.replace(",", " ").split()[0]
    for fmt in ("%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%d.%m.%Y")
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", text)
    if m:
        y = year or now_msk().year
        try:
            dt = datetime(int(y), int(m.group(2)), int(m.group(1)))
            return dt.strftime("%d.%m.%Y")
        except ValueError:
            pass
    return now_msk().strftime("%d.%m.%Y")


def _has_money(data: Dict, key: str) -> bool:
    if key not in data or data.get(key) is None:
        return False
    return str(data.get(key)).strip() not in ("", "авто", "auto", "-")


def _parse_money(raw) -> float:
    text = str(raw or "0").replace(_NBSP, " ").replace(" ", "")
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text or text in ".,-":
        return 0.0
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        parts = text.split(",")
        text = text.replace(",", "." if len(parts[-1]) <= 2 else "")
    try:
        return abs(float(text))
    except ValueError:
        digits = re.sub(r"\D", "", str(raw) or "0")
        return float(int(digits) if digits else 0)


def _fmt_money(value: float, *, signed: bool = False) -> str:
    n = float(value)
    sign = "-" if signed else ""
    body = f"{abs(n):,.2f}".replace(",", "X").replace(".", ",").replace("X", " ")
    return f"{sign}{body} RUR"


def _fmt_account(raw: str) -> str:
    from alfa_sbp_stealth import _fix_account_checksum, _gen_alfa_debit_account

    if _is_auto(raw):
        return _gen_alfa_debit_account()
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 20:
        return digits[:20]
    if len(digits) >= 10:
        return digits.ljust(20, "0")
    return _fix_account_checksum(digits or "40817810")


def _fmt_op_code(raw: str, op_date: str) -> str:
    dd, mm, yy = op_date[0:2], op_date[3:5], op_date[8:10]
    last3 = f"{random.randint(0, 999):03d}"
    if _is_auto(raw):
        mid = "".join(str(random.randint(0, 9)) for _ in range(4))
        return f"C16{dd}{mm}{yy}{mid}{last3}"
    token = re.sub(r"\s+", "", str(raw or ""))
    # Exact face codes (C… / Z… / full alphanumeric) — never rewrite last digits.
    if re.fullmatch(r"[A-Za-z]\d{14,16}", token):
        return token
    if re.fullmatch(r"[A-Za-z0-9]{12,20}", token) and re.search(r"\d", token):
        return token
    if len(token) < 4:
        mid = "".join(str(random.randint(0, 9)) for _ in range(4))
        return f"C16{dd}{mm}{yy}{mid}{last3}"
    core = re.sub(r"\d{3}$", "", token)
    if not re.search(r"\d$", core):
        core = token[:-3] if len(token) >= 3 else token
    return core + last3


def _random_address() -> str:
    index, city, street, house, flat = random.choice(_ADDR_SAFE)
    house = house + random.randint(0, 9)
    flat = flat + random.randint(0, 12)
    return f"{index}, РОССИЯ, {city}, {street}, д. {house}, кв. {flat}"


def _address_covered(u2c: Dict[int, int]) -> str:
    pool = _ADDR_POOL + _ADDR_SAFE
    for _ in range(32):
        index, city, street, house, flat = random.choice(pool)
        house = house + random.randint(0, 9)
        flat = flat + random.randint(0, 12)
        text = f"{index}, РОССИЯ, {city}, {street}, д. {house}, кв. {flat}"
        if not _missing_cps(text, u2c):
            return text
    return _random_address()


def _missing_cps(text: str, u2c: Dict[int, int]) -> str:
    miss = []
    for ch in text or "":
        if ch in ("\n", "\r", "\t"):
            continue
        cp = 0x20 if ch == " " else ord(ch)
        if cp not in u2c and (cp != 0x20 or 0x00A0 not in u2c):
            if ch not in miss:
                miss.append(ch)
    return "".join(miss)


def _needed_cover_cps() -> List[int]:
    cps = set()
    cps.update(range(ord("0"), ord("9") + 1))
    cps.update(range(ord("A"), ord("Z") + 1))
    cps.update(range(ord("a"), ord("z") + 1))
    cps.update(range(0x0410, 0x0450))
    cps.update((0x0401, 0x0451, 0x00A0, 0x2116, 0x00AB, 0x00BB))
    cps.update(ord(c) for c in " .,:;()[]{}+-/*№\"'%#@!?&_|~^<>\\$=°")
    return sorted(cps)


def _ensure_arial_cover() -> str:
    if os.path.isfile(_ARIAL_COVER) and os.path.getsize(_ARIAL_COVER) > 1000:
        return _ARIAL_COVER
    src = _WIN_ARIAL if os.path.isfile(_WIN_ARIAL) else ""
    if not src:
        raise FileNotFoundError("Arial cover missing and Windows arial.ttf not found")
    from fontTools.subset import Options, Subsetter
    from fontTools.ttLib import TTFont

    os.makedirs(os.path.dirname(_ARIAL_COVER), exist_ok=True)
    font = TTFont(src, lazy=False)
    opt = Options()
    opt.layout_features = []
    opt.name_IDs = []
    opt.notdef_outline = True
    subsetter = Subsetter(options=opt)
    subsetter.populate(unicodes=_needed_cover_cps())
    subsetter.subset(font)
    font.save(_ARIAL_COVER)
    font.close()
    return _ARIAL_COVER


def check_text(text: str) -> list:
    """Orig F1 cmap, then Arial cover (same family as donor F1)."""
    if not os.path.isfile(STATEMENT_ORIG):
        return ["?"]
    try:
        refs = _load_f1_maps(open(STATEMENT_ORIG, "rb").read())
    except Exception:
        return ["?"]
    u2c = {u: c for c, u in refs["c2u"].items() if c}
    if 0x20 not in u2c and 0x00A0 in u2c:
        u2c[0x20] = u2c[0x00A0]
    miss = _missing_cps(text or "", u2c)
    if not miss:
        return []
    try:
        from fontTools.ttLib import TTFont

        src = TTFont(_ensure_arial_cover(), lazy=False)
        cmap = src.getBestCmap() or {}
        src.close()
    except Exception:
        return list(miss)
    still = []
    for ch in miss:
        cp = 0x20 if ch == " " else ord(ch)
        if cp not in cmap:
            still.append(ch)
    return still


def _load_f1_maps(pdf: bytes):
    import fitz
    from alfa_orig_mode import _parse_bfchar, _parse_widths
    from alfa_font_extend import _ff2_read_decompressed

    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        f1 = _f1_type0_xref(doc)
        obj = doc.xref_object(f1)
        tu_xref = int(re.search(r"/ToUnicode (\d+)", obj).group(1))
        cid_xref = int(re.search(r"/DescendantFonts\s*\[\s*(\d+)", obj).group(1))
        tag = re.search(r"/BaseFont /([A-Z]{6})\+Arial", obj).group(1)
        cid_obj = doc.xref_object(cid_xref)
        fd_xref = int(re.search(r"/FontDescriptor (\d+)", cid_obj).group(1))
        fd_obj = doc.xref_object(fd_xref)
        ff2_xref = int(re.search(r"/FontFile2 (\d+)", fd_obj).group(1))
        tu = doc.xref_stream(tu_xref).decode("latin1", "replace")
        c2u = _parse_bfchar(tu)
        widths = _parse_widths(cid_obj)
        ff2 = _ff2_read_decompressed(pdf, ff2_xref)
        cs_xref = _main_cs_xref(doc)
        fx_xref = _fx0_xref(doc)
        return {
            "f1": f1,
            "tu": tu_xref,
            "cid": cid_xref,
            "fd": fd_xref,
            "ff2": ff2_xref,
            "tag": tag.encode("ascii"),
            "c2u": c2u,
            "widths": widths,
            "ff2_bytes": ff2,
            "cs": cs_xref,
            "fx": fx_xref,
            "cs_dec": doc.xref_stream(cs_xref),
            "fx_dec": doc.xref_stream(fx_xref) if fx_xref else b"",
        }
    finally:
        doc.close()


def _f1_type0_xref(doc) -> int:
    page = doc[0]
    for item in page.get_fonts():
        if item[4] == "F1":
            return int(item[0])
    raise RuntimeError("statement F1 missing")


def _main_cs_xref(doc) -> int:
    best = 0
    best_len = 0
    for xref in doc[0].get_contents() or []:
        try:
            st = doc.xref_stream(xref) or b""
        except Exception:
            continue
        if b"Tj" in st and b"/F1" in st and len(st) > best_len:
            best, best_len = int(xref), len(st)
    if not best:
        raise RuntimeError("statement content stream missing")
    return best


def _fx0_xref(doc) -> int:
    for xref in range(1, doc.xref_length()):
        try:
            st = doc.xref_stream(xref) or b""
        except Exception:
            continue
        obj = doc.xref_object(xref)
        if "/Subtype /Form" in obj and b"/F1" in st and b"Tm" in st and len(st) < 2000:
            return int(xref)
    return 0


_TF_RE = re.compile(rb"/F([123])\s+(\d+(?:\.\d+)?)\s+Tf")
_TM_RE = re.compile(rb"1 0 0 1 ([\d.]+) ([\d.]+)\s+Tm")


def _iter_paints(stream: bytes, c2u: Dict[int, int]):
    from alfa_emit import _paint_hex_span, dec_hex

    events = [("tf", m) for m in _TF_RE.finditer(stream)]
    events += [("tm", m) for m in _TM_RE.finditer(stream)]
    events.sort(key=lambda item: item[1].start())
    font, size = "F?", 0.0
    for kind, m in events:
        if kind == "tf":
            font = "F" + m.group(1).decode("ascii")
            size = float(m.group(2))
            continue
        tail = stream[m.end() : m.end() + 80]
        tf = re.match(rb"\s*/F([123])\s+(\d+(?:\.\d+)?)\s+Tf", tail)
        if tf:
            font = "F" + tf.group(1).decode("ascii")
            size = float(tf.group(2))
        hit = _paint_hex_span(stream[m.end() : m.end() + 900])
        if not hit:
            continue
        rel_a, rel_b, hx, _kind = hit
        face = dec_hex(hx, c2u) if font == "F1" else ""
        yield {
            "font": font,
            "size": size,
            "x": float(m.group(1)),
            "y": float(m.group(2)),
            "tm": m,
            "hex_a": m.end() + rel_a,
            "hex_b": m.end() + rel_b,
            "hx": hx,
            "face": face,
        }


def _key_at(y: float, x: float) -> str:
    for key, (ky, kx) in STATEMENT_COORDS.items():
        if abs(y - ky) <= 0.08 and abs(x - kx) <= 0.08:
            return key
    return ""


def _pdf_w(face: str, u2c: Dict[int, int], widths: Dict[int, int], size: float = _PT) -> float:
    total = 0.0
    for ch in face:
        cp = 0x00A0 if ch == " " else ord(ch)
        if ch == " ":
            cp = 0x20 if 0x20 in u2c else 0x00A0
        cid = u2c.get(cp) or u2c.get(0x20) or 0
        total += (widths.get(cid, 556) / 1000.0) * size
    return total


def _wrap(text: str, max_w: float, u2c, widths, nlines: int) -> List[str]:
    raw = " ".join((text or "").split())
    if not raw:
        return [" "] * nlines
    words = raw.split(" ")
    lines: List[str] = []
    cur = ""
    for word in words:
        probe = word if not cur else cur + " " + word
        if _pdf_w(probe, u2c, widths) <= max_w or not cur:
            cur = probe
            continue
        lines.append(cur)
        cur = word
        if len(lines) == nlines - 1:
            rest = " ".join([cur] + words[words.index(word) + 1 :])
            while rest and _pdf_w(rest, u2c, widths) > max_w:
                rest = rest[:-1]
            lines.append(rest or " ")
            return lines[:nlines]
    if cur:
        lines.append(cur)
    while len(lines) < nlines:
        lines.append(" ")
    return lines[:nlines]


def _hydrate_f1(orig_ttf: bytes, c2u: Dict[int, int], widths: Dict[int, int], need: str):
    from fontTools.ttLib import TTFont
    from alfa_emit import _sfnt_tables, trim_sfnt_to_aligned_end
    from alfa_font_extend import _ot_recalc_checksum_adjustment, _ot_table_checksum

    dst = TTFont(BytesIO(orig_ttf), lazy=False, recalcBBoxes=False, recalcTimestamp=False)
    src = TTFont(_ensure_arial_cover(), lazy=False)
    src_cmap = src.getBestCmap() or {}
    u2c = {u: c for c, u in c2u.items() if c}
    if 0x20 not in u2c and 0x00A0 in u2c:
        u2c[0x20] = u2c[0x00A0]
    copied = {}

    def ensure(src_name: str) -> str:
        if src_name in copied:
            return copied[src_name]
        g = deepcopy(src["glyf"][src_name])
        if getattr(g, "isComposite", lambda: False)() or getattr(g, "components", None):
            for comp in list(getattr(g, "components", []) or []):
                comp.glyphName = ensure(comp.glyphName)
        dst_name = src_name
        order = list(dst.getGlyphOrder())
        if dst_name in order:
            dst_name = f"u{len(order):04d}"
        order.append(dst_name)
        dst.setGlyphOrder(order)
        dst["glyf"][dst_name] = g
        dst["hmtx"][dst_name] = src["hmtx"].metrics[src_name]
        copied[src_name] = dst_name
        return dst_name

    upem = int(src["head"].unitsPerEm or 2048)
    for ch in need:
        if ch in ("\n", "\r", "\t"):
            continue
        cp = 0x20 if ch == " " else ord(ch)
        if cp in u2c:
            continue
        name = src_cmap.get(cp)
        if not name:
            logger.error("statement Arial missing U+%04X %r", cp, ch)
            src.close()
            dst.close()
            return None, {}, {}, {}
        ensure(name)
        cid = dst.getGlyphID(copied[name])
        c2u[cid] = cp
        u2c[cp] = cid
        aw = int(src["hmtx"].metrics[name][0])
        widths[cid] = int(round(aw * 1000 / upem))

    dst["maxp"].numGlyphs = len(dst.getGlyphOrder())
    buf = BytesIO()
    dst.save(buf)
    dst.close()
    src.close()
    tables = _sfnt_tables(buf.getvalue())
    keep = ["cvt ", "fpgm", "glyf", "head", "hhea", "hmtx", "loca", "maxp", "prep"]
    packed_tables = {t: tables[t] for t in keep if t in tables}
    import math
    import struct

    order = [t for t in keep if t in packed_tables]
    n = len(order)
    entry_selector = int(math.log2(n)) if n else 0
    search_range = (2 ** entry_selector) * 16
    range_shift = n * 16 - search_range
    header = b"\x00\x01\x00\x00" + struct.pack(
        ">HHHH", n, search_range, entry_selector, range_shift,
    )
    current = 12 + n * 16
    body = b""
    recs = []
    for tag in order:
        payload = packed_tables[tag]
        pad = b"" if tag == "glyf" else (b"\x00" * ((4 - (len(payload) % 4)) % 4))
        recs.append((tag, current, len(payload), _ot_table_checksum(payload)))
        body += payload + pad
        current += len(payload) + len(pad)
    directory = b""
    for tag, off, length, checksum in recs:
        directory += tag.encode("latin-1") + struct.pack(">III", checksum, off, length)
    ttf = trim_sfnt_to_aligned_end(_ot_recalc_checksum_adjustment(header + directory + body))
    return ttf, u2c, dict(c2u), dict(widths)


def _enc(face: str, u2c: Dict[int, int]) -> bytes:
    out = []
    for ch in face:
        cp = 0x20 if ch == " " else (0x00A0 if ch == _NBSP else ord(ch))
        cid = u2c.get(cp)
        if cid is None and cp == 0x20:
            cid = u2c.get(0x00A0)
        if cid is None:
            return b""
        out.append(f"{cid:04X}")
    return "".join(out).encode("ascii")


def _fmt_tm_x(x: float) -> bytes:
    text = f"{x:.3f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text.encode("ascii")


def _rewrite_stream(
    stream: bytes,
    c2u_old: Dict[int, int],
    u2c: Dict[int, int],
    values: Dict[str, str],
    widths: Dict[int, int],
) -> bytes:
    paints = list(_iter_paints(stream, c2u_old))
    edits: List[Tuple[int, int, bytes]] = []
    for p in paints:
        if p["font"] != "F1":
            continue
        key = _key_at(p["y"], p["x"])
        face = values.get(key, p["face"])
        hx = _enc(face, u2c)
        if not hx:
            raise ValueError(f"encode fail {key or p['face']!r}")
        edits.append((p["hex_a"], p["hex_b"], hx))
        if key in _RIGHT_KEYS:
            new_x = _RIGHT_EDGE - _pdf_w(face, u2c, widths)
            tm = p["tm"]
            edits.append((tm.start(1), tm.end(1), _fmt_tm_x(new_x)))
    buf = bytearray(stream)
    for a, b, payload in sorted(edits, key=lambda t: t[0], reverse=True):
        buf[a:b] = payload
    return bytes(buf)


def _put_stream(pdf: bytearray, xref: int, decoded: bytes) -> bool:
    from alfa_font_extend import _find_stream_pos_for_xref, _patch_length_and_rebuild
    from alfa_orig_mode import _unreproducible_flate, _zlib_level_from_header

    pos = _find_stream_pos_for_xref(bytes(pdf), xref)
    if not pos:
        return False
    cs, ce = pos
    level = _zlib_level_from_header(bytes(pdf)[cs:cs + 2]) or 6
    compressed = _unreproducible_flate(decoded, level)
    patched = _patch_length_and_rebuild(pdf, cs, ce, compressed)
    if patched is None:
        return False
    pdf[:] = patched
    return True


def _new_prefix(old: bytes) -> bytes:
    while True:
        tag = "".join(random.choice(string.ascii_uppercase) for _ in range(6)).encode("ascii")
        if tag != old:
            return tag


def _serialize_w_statement(widths: Dict[int, int]) -> bytes:
    """Keep orig CIDFont dialect: `cid [ w ]` with spaces, wrap ~6/line."""
    items = [f"{cid} [ {int(widths[cid])} ]" for cid in sorted(widths)]
    lines = []
    for i in range(0, len(items), 6):
        chunk = " ".join(items[i : i + 6])
        lines.append(chunk if i == 0 else "      " + chunk)
    return ("/W [ " + "\n".join(lines) + " ]").encode("ascii")


def _apply_f1_prefix(raw: bytes, old: bytes, new: bytes) -> bytes:
    """Swap F1 `XXXXXX+Arial` only — never F2/F3 `+Arial#20Italic/Bold`."""
    pat = re.compile(rb"/" + re.escape(old) + rb"\+Arial(?![A-Za-z0-9#])")
    out, n = pat.subn(b"/" + new + b"+Arial", raw)
    if n != 3:
        logger.warning("statement F1 prefix hits=%d want=3", n)
    return out


def _install_f1(
    pdf: bytes,
    ttf: bytes,
    c2u: Dict[int, int],
    widths: Dict[int, int],
    prefix: bytes,
) -> Optional[bytes]:
    """Rewrite F1 FontFile2 / ToUnicode / W / prefix. Leave F2/F3 bytes alone."""
    from alfa_emit import _force_length1, _install_ff2_rebuild, _rebuild_tounicode, sfnt_has_exact_aligned_end
    from alfa_font_extend import (
        _ff2_read_decompressed,
        _ot_checksum_matches,
        _patch_tu_decompressed,
    )
    from tbank_orig_mode import find_object_range
    from tbank_sbp_stealth import _replace_byte_range_and_rebuild

    if not ttf or not sfnt_has_exact_aligned_end(ttf):
        logger.error("statement F1 sfnt-tail")
        return None
    if not _ot_checksum_matches(ttf):
        logger.error("statement F1 ot-csa")
        return None
    refs0 = _load_f1_maps(pdf)
    raw = _apply_f1_prefix(pdf, refs0["tag"], prefix)
    buf = bytearray(raw)
    refs = _load_f1_maps(bytes(buf))
    if not _install_ff2_rebuild(buf, refs["ff2"], ttf, flate_level=6):
        logger.error("statement F1 FontFile2 rebuild failed")
        return None
    refs = _load_f1_maps(bytes(buf))
    new_tu = _rebuild_tounicode(b"", c2u)
    if not _patch_tu_decompressed(buf, refs["tu"], new_tu):
        logger.error("statement F1 ToUnicode rebuild failed")
        return None
    refs = _load_f1_maps(bytes(buf))
    rng = find_object_range(bytes(buf), refs["cid"])
    if not rng:
        logger.error("statement F1 CIDFont missing")
        return None
    obj = bytes(buf[rng[0] : rng[1]]).decode("latin1", "replace")
    w_full = _serialize_w_statement(widths).decode("ascii")
    m_w = re.search(r"/W\s*\[", obj)
    if not m_w:
        logger.error("statement F1 /W missing")
        return None
    i = m_w.end() - 1
    depth = 0
    br = None
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
        logger.error("statement F1 /W span")
        return None
    new_obj = obj[: br[0]] + w_full + obj[br[1] :]
    patched = _replace_byte_range_and_rebuild(
        bytes(buf), rng[0], rng[1], new_obj.encode("latin1"),
    )
    if patched is None:
        logger.error("statement F1 /W rebuild failed")
        return None
    buf = bytearray(patched)
    refs = _load_f1_maps(bytes(buf))
    landed = _ff2_read_decompressed(bytes(buf), refs["ff2"])
    if not landed or not sfnt_has_exact_aligned_end(landed) or not _ot_checksum_matches(landed):
        logger.error("statement F1 landed font broken")
        return None
    if not _force_length1(buf, refs["ff2"], len(landed)):
        logger.error("statement F1 Length1 failed")
        return None
    return bytes(buf)


def _itext_stamp_ids(pdf: bytes) -> bytes:
    """App statement is iText PdfStamper: /ID pair is unequal."""
    a = f"{random.getrandbits(128):032x}".encode('ascii')
    b = f"{random.getrandbits(128):032x}".encode('ascii')
    while b == a:
        b = f"{random.getrandbits(128):032x}".encode('ascii')
    out, n = re.subn(
        rb"/ID\s*\[\s*<[0-9A-Fa-f]{32}>\s*<[0-9A-Fa-f]{32}>\s*\]",
        b"/ID [<" + a + b"><" + b + b">]",
        pdf,
        count=1,
    )
    if n != 1:
        logger.warning('Alfa statement /ID replace failed')
        return pdf
    return out


def _touch_itext_moddate(pdf: bytes) -> bytes:
    stamp = now_msk().strftime("D:%Y%m%d%H%M%S+03'00'")
    out, n = re.subn(
        rb"/ModDate\s*\(D:[^)]+\)",
        f"/ModDate ({stamp})".encode('ascii'),
        pdf,
        count=1,
    )
    if n != 1:
        logger.warning('Alfa statement ModDate replace failed')
        return pdf
    return out


def _prepare(data: Dict) -> Dict[str, str]:
    formed = _parse_date(data.get("date_formed") or data.get("date_time") or data.get("date"))
    year = int(formed[6:10])
    period_from = _parse_date(data.get("period_from"), year=year)
    period_to = _parse_date(data.get("period_to"), year=year)
    op_date = _parse_date(data.get("op_date") or period_from, year=year)
    amount = _parse_money(data.get("amount") or data.get("expenses") or "0")
    incoming = _parse_money(data.get("incoming")) if _has_money(data, "incoming") else _INCOMING_ORIG
    outgoing = (
        _parse_money(data.get("outgoing"))
        if _has_money(data, "outgoing")
        else max(incoming - amount, 0.0)
    )
    pay_limit = _parse_money(data.get("pay_limit")) if _has_money(data, "pay_limit") else outgoing
    current_balance = (
        _parse_money(data.get("current_balance"))
        if _has_money(data, "current_balance")
        else outgoing
    )
    account = _fmt_account(str(data.get("account") or ""))
    client = " ".join(str(data.get("client") or data.get("receiver") or "").split())
    if not client:
        raise ValueError("empty client")
    addr_raw = str(data.get("address") or "")
    address = _random_address() if _is_auto(addr_raw) else " ".join(addr_raw.split())
    op_code = _fmt_op_code(str(data.get("operation_num") or data.get("op_code") or ""), op_date)
    desc = str(data.get("message") or data.get("description") or "").strip()
    if _is_auto(desc):
        desc = f"Перевод {op_code} через Систему быстрых платежей. Без НДС."
    elif "{op}" in desc:
        desc = desc.replace("{op}", op_code)
    return {
        "account": account,
        "date_formed": formed,
        "client": client,
        "address": address,
        "period": f"За период с {period_from} по {period_to}",
        "incoming": _fmt_money(incoming),
        "expenses": _fmt_money(amount),
        "outgoing": _fmt_money(outgoing),
        "pay_limit": _fmt_money(pay_limit),
        "current_balance": _fmt_money(current_balance),
        "op_date": op_date,
        "op_code": op_code,
        "description": desc,
        "tx_amount": _fmt_money(amount, signed=True),
        "hold_1": (
            "Неподтвержденная операция: Кумулятивная сумма, дата операции: "
            f"{op_date}, "
        ),
    }


def create_alfa_statement_stealth(
    data: Dict,
    *,
    allow_repeat: bool = False,
    allow_ff2_repeat: bool = False,
    claim_minute: bool = True,
    shells: Optional[list] = None,
) -> Optional[bytes]:
    """Emit onto the app statement shell (Oracle BI + iText 4.2 stamper)."""
    del allow_repeat, allow_ff2_repeat, claim_minute, shells

    def _strip_pdf_eof_tail(pdf: bytes) -> bytes:
        eof_idx = pdf.rfind(b"%%EOF")
        if eof_idx < 0:
            return pdf
        end = eof_idx + 5
        if pdf[end:end + 2] == b"\r\n":
            end += 2
        elif end < len(pdf) and pdf[end:end + 1] in (b"\n", b"\r"):
            end += 1
        if end < len(pdf) and pdf[end:].strip(b"\x00 \t\r\n") == b"":
            return pdf[:end]
        return pdf

    if not os.path.isfile(STATEMENT_ORIG):
        logger.error("Alfa statement orig missing: %s", STATEMENT_ORIG)
        return None
    try:
        prepared = _prepare(data)
    except Exception as exc:
        logger.error("Alfa statement prepare: %s", exc)
        return None

    shell = open(STATEMENT_ORIG, "rb").read()
    refs = _load_f1_maps(shell)
    c2u = dict(refs["c2u"])
    widths = dict(refs["widths"])
    u2c = {u: c for c, u in c2u.items() if c}
    if 0x20 not in u2c and 0x00A0 in u2c:
        u2c[0x20] = u2c[0x00A0]
    values = dict(prepared)
    need = "".join(values.values())
    for p in _iter_paints(refs["cs_dec"], c2u):
        if p["font"] == "F1":
            need += p["face"]
    for p in _iter_paints(refs["fx_dec"], c2u):
        if p["font"] == "F1":
            need += p["face"]

    new_ttf = None
    miss_face = _missing_cps(need, u2c)
    if miss_face:
        extra = ""
        if _is_auto(str(data.get("address") or "")):
            extra = "РОССИЯ, д. кв. " + "".join(
                str(x) for row in _ADDR_POOL for x in row
            )
        new_ttf, u2c, c2u, widths = _hydrate_f1(
            refs["ff2_bytes"], dict(c2u), dict(widths), miss_face + extra,
        )
        if not new_ttf:
            logger.error("Alfa statement orig F1 cannot paint: %s", miss_face)
            return None

    if _is_auto(str(data.get("address") or "")):
        prepared["address"] = _address_covered(u2c)
        values = dict(prepared)

    addr_lines = _wrap(prepared["address"], _ADDR_MAX_W, u2c, widths, 4)
    desc_lines = _wrap(prepared["description"], _DESC_MAX_W, u2c, widths, 2)
    values["address_1"], values["address_2"], values["address_3"], values["address_4"] = addr_lines
    values["desc_1"], values["desc_2"] = desc_lines

    try:
        new_cs = _rewrite_stream(refs["cs_dec"], refs["c2u"], u2c, values, widths)
        new_fx = _rewrite_stream(refs["fx_dec"], refs["c2u"], u2c, values, widths) if refs["fx_dec"] else b""
    except Exception as exc:
        logger.error("Alfa statement rewrite: %s", exc)
        return None

    pdf = bytearray(shell)

    refs2 = _load_f1_maps(bytes(pdf))
    if not _put_stream(pdf, refs2["cs"], new_cs):
        logger.error("Alfa statement content rebuild failed")
        return None
    refs2 = _load_f1_maps(bytes(pdf))
    if new_fx and refs2["fx"] and not _put_stream(pdf, refs2["fx"], new_fx):
        logger.error("Alfa statement fx0 rebuild failed")
        return None
    out = bytes(pdf)
    if new_ttf:
        installed = _install_f1(out, new_ttf, c2u, widths, _new_prefix(refs["tag"]))
        if not installed:
            logger.error("Alfa statement F1 hydrate install failed")
            return None
        out = installed
    out = _touch_itext_moddate(out)
    out = _itext_stamp_ids(out)
    return _strip_pdf_eof_tail(out)
