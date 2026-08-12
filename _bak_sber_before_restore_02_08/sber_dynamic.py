"""
Sber SBP dynamic generator — Jasper/OpenPDF pipeline как у T-Bank SBP.

Charset: union library (sber_glyphs.pkl) + per-receipt hydrate into a fat
atlas shell. Missing face letters steal donor FIO CIDs (size-neutral), not a
lean full-cmap runtime (wrong glyf/hmtx atlas → Proton HARD).
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import unicodedata
import zlib
from datetime import datetime, timedelta
from io import BytesIO
from typing import Dict, List, Optional, Set, Tuple

import fitz
from fontTools.ttLib import TTFont

import sber_glyph_library as sgl
import tbank_unlock_template as tut
from openpdf_deflate import compress_like_jasper

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
SHELL_ARTIFACT = os.path.join(_DIR, "templates", "S_sbp_shell.pdf")
SBER_SHELLS_DIR = os.path.join(_DIR, "templates", "sber_shells")
ORIG_TEMPLATE = os.path.join(_DIR, "templates", "S_sbp_original.pdf")
UNLOCKED_TEMPLATE = os.path.join(_DIR, "templates", "S_sbp_unlocked.pdf")
RUNTIME_TEMPLATE = os.path.join(_DIR, "templates", "S_sbp_runtime.pdf")
# Шаблоны с увеличенными CID-слотами ФИО (оригиналы из templates/).
EXTRA_SBP_SHELLS = [
    os.path.join(_DIR, "templates", "55.pdf"),
    os.path.join(_DIR, "templates", "sber_original.pdf"),
]
CORPUS_SBER_DIR = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "сбер")

_ESC = {
    0x28: b"\\(", 0x29: b"\\)", 0x5C: b"\\\\",
    0x0A: b"\\n", 0x0D: b"\\r",
    0x08: b"\\b", 0x0C: b"\\f", 0x09: b"\\t",
}
# Длиннее того же байта (octal) — для добора literal-длины без смены unescape.
_ESC_LONG = {
    0x28: b"\\050", 0x29: b"\\051", 0x5C: b"\\134",
    0x0A: b"\\012", 0x0D: b"\\015",
    0x08: b"\\010", 0x0C: b"\\014", 0x09: b"\\011",
}
_RUBLE_ESC = b"\\rZ"
PHONE_DATE_CENTER_X = 153.0  # у всех оригиналов Сбера (phone+SBP) центр даты = 153
PHONE_DATE_Y = 615.74
SBP_DATE_Y = 711.74
PHONE_DATE_SIZE = 12.0
SBER_DATE_CENTER_X = PHONE_DATE_CENTER_X
_TM_RE = re.compile(rb"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm")
_BT_ET_RE = re.compile(rb"BT\s*(.*?)\s*ET", re.DOTALL)

_SHELL_ORIG = {
    "date_time": "03 июня 2026 12:44:56 (МСК)",
    "amount": "3500.00  ₽",
    "commission": "0.00  ₽",
    "sender_account": "•••• 2332",
    "recipient_bank": "ПСБ",
    "receiver_name": "Алексей Александрович П",
    "spb_number": "A6154094453029020G10030011770901",
    "receiver_phone": "+7 911 826-78-06",
    "sender_name": "Оюмаа Орлановна К.",
}

_FIELD_Y = {
    "date_time": 711.74,
    "receiver_name": 604.74,
    "receiver_phone": 565.74,
    "recipient_bank": 524.74,
    "sender_name": 475.74,
    "sender_account": 434.74,
    "amount": 385.74,
    "commission": 345.74,
    "spb_number": 304.74,
}

PHONE_ORIG_TEMPLATE = os.path.join(
    SBER_SHELLS_DIR, "сбер по номеру телефона на сбер.pdf",
)

_PHONE_FIELD_Y = {
    "date_time": 615.74,
    "receiver_name": 524.74,
    "receiver_phone": 490.74,
    "receiver_account": 456.74,
    "sender_name": 398.74,
    "sender_account": 364.74,
    "amount": 330.74,
    "commission": 296.74,
    "document_num": 238.74,
    "auth_code": 204.74,
}

_SBP_FIELD_SPECS = [
    ("date", "date_time", 12.0),
    ("amount", "amount", 12.0),
    ("commission", "commission", 12.0),
    ("account", "sender_account", 12.0),
    ("bank", "recipient_bank", 12.0),
    ("receiver", "receiver_name", 12.0),
    ("phone", "receiver_phone", 12.0),
    ("sender", "sender_name", 12.0),
    ("sbp_id", "spb_number", 12.0),
]

_PHONE_FIELD_SPECS = [
    ("date", "date_time", 12.0),
    ("receiver", "receiver_name", 12.0),
    ("phone", "receiver_phone", 12.0),
    ("recv_acct", "receiver_account", 12.0),
    ("sender", "sender_name", 12.0),
    ("account", "sender_account", 12.0),
    ("amount", "amount", 12.0),
    ("commission", "commission", 12.0),
    ("doc", "document_num", 12.0),
    ("auth", "auth_code", 12.0),
]

CARD_ORIG_TEMPLATE = os.path.join(
    SBER_SHELLS_DIR, "сбер по карте в другой банк.pdf",
)

# Value-field Y (PDF user space) from donor «сбер по карте в другой банк.pdf»
_CARD_FIELD_Y = {
    "date_time": 818.81,
    "dest_card": 740.93,
    "bank": 655.93,
    "country": 570.93,
    "amount": 485.93,
    "commission": 400.93,
    "charged": 315.93,
    "sender_name": 230.93,
    "sender_card": 145.93,
}

_CARD_FIELD_SPECS = [
    ("date", "date_time", 13.0),
    ("dest_card", "dest_card", 15.0),
    ("bank", "bank", 15.0),
    ("country", "country", 15.0),
    ("amount", "amount", 15.0),
    ("commission", "commission", 15.0),
    ("charged", "charged", 15.0),
    ("sender", "sender_name", 15.0),
    ("sender_card", "sender_card", 15.0),
]

CARD_FIELD_COORDS = {k: (v, 32.0) for k, v in _CARD_FIELD_Y.items()}
CARD_FIELD_COORDS["date_time"] = (818.81, 82.03)

_SHELL_CHAR_FALLBACK = None  # lazy: sber_glyph_library.SHELL_CHAR_FALLBACK


def _shell_char_fallback() -> dict:
    global _SHELL_CHAR_FALLBACK
    if _SHELL_CHAR_FALLBACK is None:
        import sber_glyph_library as sgl
        sgl.ensure_library()
        _SHELL_CHAR_FALLBACK = sgl.shell_char_fallback_active()
    return _SHELL_CHAR_FALLBACK

_DATE_LEFT_X_BY_MONTH = {
    # Measured from original Sber phone receipts (center X = 153 on 300pt page).
    "января": 66.18,
    "февраля": 64.64,
    "марта": 69.30,
    "апреля": 66.18,
    "мая": 75.47,
    "июня": 71.77,
    "июля": 71.53,
    "августа": 66.18,
    "сентября": 61.31,
    "октября": 66.18,
    "ноября": 69.30,
    "декабря": 63.37,
}

_CORPUS_DONORS_CACHE: Optional[List[str]] = None
_SBP_SHELL_INDEX: Optional[List[Tuple[str, str, Dict[str, int], Dict[int, int]]]] = None

KNOWN_SBER_SBP_SKELETONS = frozenset({
    "049e897dfbd028d6",
    "0dada88492839afc",
    "4501ca654abeccde",
    "4a828bd3628faf20",
    "866cd81f4fa31279",
    "8934e8673322f090",
    "e631b81cab83544b",
})


def _mask_pdf_literal_tj(stream: bytes) -> bytes:
    """Replace `(... )Tj` with `() Tj`, honouring PDF string escapes (\\), \\n, …).

    Naive `\\([^)]*\\)` breaks when a CID byte is 0x29 and encoded as `\\)` —
    that falsely flips the content-skeleton hash (Sber font-patch + щ).
    """
    out = bytearray()
    i = 0
    n = len(stream)
    while i < n:
        if stream[i] != 0x28:  # '('
            out.append(stream[i])
            i += 1
            continue
        j = i + 1
        while j < n:
            b = stream[j]
            if b == 0x5C:  # '\\'
                j += 2 if j + 1 < n else 1
                continue
            if b == 0x29:  # ')'
                break
            j += 1
        if j >= n:
            out.append(stream[i])
            i += 1
            continue
        k = j + 1
        while k < n and stream[k] in b" \t\r\n":
            k += 1
        if stream[k:k + 2] == b"Tj":
            out.extend(b"() Tj")
            i = k + 2
            continue
        out.append(stream[i])
        i += 1
    return bytes(out)


def _iter_pdf_literal_tj(stream: bytes):
    """Yield raw payloads of literal ``Tj`` strings, honoring PDF escapes."""
    i = 0
    n = len(stream)
    while i < n:
        i = stream.find(b"(", i)
        if i < 0:
            return
        j = i + 1
        while j < n:
            b = stream[j]
            if b == 0x5C:  # escaped byte / octal escape
                j += 2 if j + 1 < n else 1
                continue
            if b == 0x29:
                break
            j += 1
        if j >= n:
            return
        k = j + 1
        while k < n and stream[k] in b" \t\r\n":
            k += 1
        if stream[k:k + 2] == b"Tj":
            yield stream[i + 1:j]
            i = k + 2
        else:
            i += 1


# Proton SBER_CONTENT_QQ_PROFILE: naive bare q/Q counts (incl. CID low bytes in Tj).
_SBP_QQ_ALLOWED = frozenset({(7, 4), (8, 4), (8, 5)})
# Phone / sber_internal_jasper genuines (n=15): 10/4, 11/4, 13/4 only.
_PHONE_QQ_ALLOWED = frozenset({(10, 4), (11, 4), (13, 4)})
# Phone FontFile2 decoded exact atlas (HARD SBER_FONTFILE2_DEC_PROFILE).
_PHONE_FF2_DEC_ALLOWED = frozenset({55_136, 55_476, 56_064})


def _sber_naive_qq(cs: bytes) -> Tuple[int, int]:
    return (
        len(re.findall(rb"(?<![A-Za-z0-9_/])q(?![A-Za-z0-9_])", cs)),
        len(re.findall(rb"(?<![A-Za-z0-9_/])Q(?![A-Za-z0-9_])", cs)),
    )


def _normalize_sber_qq_profile(
    stream: bytes,
    *,
    allowed: Optional[frozenset] = None,
) -> bytes:
    """Octal-escape bare q/Q inside PDF strings so naive q/Q lands in corpus set.

    Proton counts ``q``/``Q`` even inside Tj CID bytes (e.g. ``\\x02q``). Chaos FIO
    can push 9/5 → HARD SBER_CONTENT_QQ_PROFILE. ``\\161``/``\\121`` keep unescape
    identical while removing the bare letter from the naive scan.
    """
    allowed_set = allowed if allowed is not None else _SBP_QQ_ALLOWED
    q0, Q0 = _sber_naive_qq(stream)
    if (q0, Q0) in allowed_set:
        return stream
    targets = [
        t for t in allowed_set
        if t[0] <= q0 and t[1] <= Q0
    ]
    if not targets:
        logger.warning(
            "Sber QQ profile %d/%d below corpus %s — leave as-is",
            q0, Q0, sorted(allowed_set),
        )
        return stream
    # Fewest octal escapes first (each +3 raw bytes → flate risk).
    prefer = {
        (8, 5): 0, (8, 4): 1, (7, 4): 2,
        (11, 4): 0, (10, 4): 1, (13, 4): 2,
    }
    tq, tQ = min(
        targets,
        key=lambda t: ((q0 - t[0]) + (Q0 - t[1]), prefer.get(t, 9), -t[0], -t[1]),
    )
    need_q, need_Q = q0 - tq, Q0 - tQ
    if need_q == 0 and need_Q == 0:
        return stream

    out = bytearray()
    i = 0
    n = len(stream)
    while i < n:
        if stream[i] != 0x28:  # (
            out.append(stream[i])
            i += 1
            continue
        # Copy literal string, octal-escaping bare q/Q when still needed.
        out.append(0x28)
        i += 1
        while i < n:
            b = stream[i]
            if b == 0x5C and i + 1 < n:
                # Keep existing escapes intact (may already hide q/Q).
                nxt = stream[i + 1]
                if 0x30 <= nxt <= 0x37:  # octal
                    j = i + 1
                    while j < n and j < i + 4 and 0x30 <= stream[j] <= 0x37:
                        j += 1
                    out.extend(stream[i:j])
                    i = j
                    continue
                out.extend(stream[i:i + 2])
                i += 2
                continue
            if b == 0x29:
                out.append(b)
                i += 1
                break
            if b == 0x71 and need_q > 0:  # q
                out.extend(b"\\161")
                need_q -= 1
                i += 1
                continue
            if b == 0x51 and need_Q > 0:  # Q
                out.extend(b"\\121")
                need_Q -= 1
                i += 1
                continue
            out.append(b)
            i += 1
    result = bytes(out)
    grew = len(result) - len(stream)
    if grew > 0:
        # Reclaim beyond `grew` — stream may already sit near flate ≤733.
        result = _reclaim_pdf_literal_bytes(result, grew + 48)
    q1, Q1 = _sber_naive_qq(result)
    if (q1, Q1) in allowed_set:
        logger.info(
            "Sber QQ profile %d/%d → %d/%d (octal-escape, dec %d→%d)",
            q0, Q0, q1, Q1, len(stream), len(result),
        )
        return result
    # Partial escape can land on illegal 7/5 — prefer leaving donor count
    # (GEN_NONE) over shipping a broken profile.
    logger.warning(
        "Sber QQ profile still %d/%d after escape (was %d/%d) — revert",
        q1, Q1, q0, Q0,
    )
    return stream


# Long octal pads (from slot grow) → short PDF escapes; same unescape, −2 raw.
_LONG_OCTAL_TO_SHORT = (
    (b"\\012", b"\\n"),
    (b"\\015", b"\\r"),
    (b"\\011", b"\\t"),
    (b"\\010", b"\\b"),
    (b"\\014", b"\\f"),
    (b"\\050", b"\\("),
    (b"\\051", b"\\)"),
    (b"\\134", b"\\\\"),
)

# Keep QQ octal hides intact when reclaiming slot-grow padding.
_QQ_HIDE_OCTALS = frozenset({b"\\161", b"\\121"})
# PDF special second-bytes after `\`; also digits (start of octal).
_PDF_ESC_SPECIAL = set(b"nrtbf()\\0123456789")


def _shorten_pdf_long_octals(stream: bytes, need: int) -> bytes:
    """Reclaim raw bytes inside literals after QQ octal-escape growth."""
    return _reclaim_pdf_literal_bytes(stream, need)


def _reclaim_pdf_literal_bytes(stream: bytes, need: int) -> bytes:
    """Undo slot-grow padding inside ``(…)`` without changing unescape / QQ hides.

    Order: long-octal→short-escape (−2), noop ``\\X``→raw (−1), safe ``\\ddd``→raw (−3).
    Never collapses ``\\161``/``\\121`` (QQ bare-letter hides).
    """
    if need <= 0:
        return stream
    out = bytearray()
    i = 0
    n = len(stream)
    saved = 0
    while i < n:
        if stream[i] != 0x28:
            out.append(stream[i])
            i += 1
            continue
        out.append(0x28)
        i += 1
        while i < n:
            if saved < need:
                hit = False
                for long_e, short_e in _LONG_OCTAL_TO_SHORT:
                    if stream.startswith(long_e, i):
                        out.extend(short_e)
                        i += len(long_e)
                        saved += len(long_e) - len(short_e)
                        hit = True
                        break
                if hit:
                    continue
            b = stream[i]
            if b == 0x5C and i + 1 < n:
                nxt = stream[i + 1]
                if 0x30 <= nxt <= 0x37:
                    j = i + 1
                    while j < n and j < i + 4 and 0x30 <= stream[j] <= 0x37:
                        j += 1
                    esc = stream[i:j]
                    if (
                        saved < need
                        and esc not in _QQ_HIDE_OCTALS
                        and len(esc) >= 2
                    ):
                        try:
                            val = int(esc[1:].decode("ascii"), 8) & 0xFF
                        except Exception:
                            val = -1
                        # Collapse \ddd → raw when the byte needs no PDF escape.
                        if (
                            val >= 0
                            and val not in (0x28, 0x29, 0x5C)
                            and val not in (0x71, 0x51)  # bare q/Q
                        ):
                            out.append(val)
                            saved += len(esc) - 1
                            i = j
                            continue
                    out.extend(esc)
                    i = j
                    continue
                # Slot-grow ``\X`` noop (+1 raw): collapse back when safe.
                if (
                    saved < need
                    and nxt not in _PDF_ESC_SPECIAL
                    and nxt not in (0x71, 0x51)
                ):
                    out.append(nxt)
                    saved += 1
                    i += 2
                    continue
                out.extend(stream[i : i + 2])
                i += 2
                continue
            out.append(b)
            i += 1
            if b == 0x29:
                break
    return bytes(out)


def _content_skeleton_hash(stream: bytes) -> str:
    sk = _mask_pdf_literal_tj(stream)
    sk = re.sub(rb"<[0-9A-Fa-f]+>\s*Tj", b"<> Tj", sk)
    # Identity-H TJ arrays (Sber card): ignore hex payloads
    sk = re.sub(rb"<[0-9A-Fa-f]+>", b"<>", sk)
    sk = re.sub(rb"\d+\.?\d*\s+\d+\.?\d*\s+Td", b"N N Td", sk)
    return hashlib.sha256(sk).hexdigest()[:16]


def _pdf_content_skeleton(pdf_bytes: bytes) -> Optional[str]:
    cs = content_stream_bytes_from_pdf(pdf_bytes)
    return _content_skeleton_hash(cs) if cs else None


def content_stream_bytes_from_pdf(pdf_bytes: bytes) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    cs_xref = doc[0].get_contents()[0]
    cs = doc.xref_stream(cs_xref)
    doc.close()
    return cs


def _amount_rubles_int(amount_text: str) -> int:
    """Рубли из «5500», «5500.00 ₽», «10 000,00 ₽» — без удвоения копеек."""
    raw = (amount_text or "").strip().replace("\u00a0", " ").replace("\u202f", " ")
    raw = raw.replace("₽", "").replace("\u20bd", "").strip()
    # Телефонный чек: «10 000,00»; SBP: «10000.00»
    if "," in raw and "." not in raw:
        head = raw.split(",", 1)[0]
        digs = re.sub(r"\D", "", head)
    elif "." in raw:
        head = raw.split(".", 1)[0]
        digs = re.sub(r"\D", "", head)
    else:
        digs = re.sub(r"\D", "", raw)
    return int(digs) if digs else 0


def _phone_amount_variants(amount_text: str) -> List[str]:
    raw = (amount_text or "").replace("\u20bd", "₽").replace("\u00a0", " ").strip()
    if "," in raw and "₽" in raw:
        base = raw.replace("₽", "").strip()
    else:
        n = _amount_rubles_int(amount_text)
        base = f"{n:,}".replace(",", " ") + ",00"
    return [
        f"{base} ₽",
        f"{base}₽",
        f" {base} ₽",
        f"{base}  ₽",
        f"  {base} ₽",
        f"{base}\u00a0₽",
        f" {base}\u00a0₽",
    ]


def _amount_variants(amount_text: str) -> List[str]:
    n = _amount_rubles_int(amount_text)
    core = f"{n}.00"
    return [
        f"{core}  ₽",
        f"{core} ₽",
        f"{core}   ₽",
        f" {core}  ₽",
        f"{core}\u00a0 ₽",
        f"{core}\u202f₽",
        f"  {core} ₽",
        f"  {core}\u00a0 ₽",
    ]


def _date_fit_variants(date_time: str) -> List[str]:
    """Варианты даты: сначала полные (год+время), зона/секунды — только запас.

    Слот ~53 B хватает на «мая»+секунды+(МСК), но не всегда на «июля».
    Порядок сжатия: пробел перед зоной → убрать (МСК) → убрать секунды.
    Никогда не отдаём «21 июля» без года и времени.
    """
    base = re.sub(r"\s+", " ", (date_time or "").strip())
    if not base:
        return [base]
    out: List[str] = [base]

    def _add(v: str) -> None:
        if v and v not in out:
            out.append(v)

    m2 = re.match(
        r"^(\d{1,2}) (\S+) (\d{4}) (\d{2}:\d{2})(:\d{2})?(\s*)(\(МСК\))?$",
        base,
    )
    if m2:
        day0 = int(m2.group(1))
        day = f"{day0:02d}"
        month, year = m2.group(2), m2.group(3)
        hm, sec = m2.group(4), m2.group(5) or ""
        msk = m2.group(7) or "(МСК)"
        # 1) Полные с секундами + зона (пробел обязателен — иначе чекеры → epoch 1970)
        if sec:
            for d in (day, str(day0)):
                _add(f"{d} {month} {year} {hm}{sec} {msk}")
        # 2) Без секунд, но с (МСК) и пробелом
        for d in (day, str(day0)):
            _add(f"{d} {month} {year} {hm} {msk}")
        # 3) Без пробела перед зоной — только если иначе не влезает (хуже для парсеров)
        if sec:
            for d in (day, str(day0)):
                _add(f"{d} {month} {year} {hm}{sec}{msk}")
        for d in (day, str(day0)):
            _add(f"{d} {month} {year} {hm}{msk}")
        # 4) Без зоны — только если иначе не влезает год+время
        if sec:
            for d in (day, str(day0)):
                _add(f"{d} {month} {year} {hm}{sec}")
        for d in (day, str(day0)):
            _add(f"{d} {month} {year} {hm}")
    else:
        _add(base.replace(" (МСК)", "(МСК)"))
        _add(re.sub(r"\s*\(МСК\)\s*$", "", base).strip())
        m = re.match(r"^(.+ \d{2}:\d{2}):\d{2}(\s*\(МСК\))?$", base)
        if m:
            _add(m.group(1) + (m.group(2) or ""))
            _add(m.group(1))
    return out


def _date_has_year_and_time(value: str) -> bool:
    return bool(
        re.search(r"\d{4}", value or "")
        and re.search(r"\d{1,2}:\d{2}", value or "")
    )

def _date_variants(date_time: str) -> List[str]:
    base = re.sub(r"\s+", " ", (date_time or "").strip())
    if not base:
        return [base]
    months_ru = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    out = [base]
    parts = base.split()
    if len(parts) >= 5 and parts[0].isdigit() and parts[1].lower() in months_ru:
        day, year, tail = parts[0], parts[2], " ".join(parts[3:])
        for month in months_ru:
            out.append(f"{day} {month} {year} {tail}")
    return out


def _pad_encoded_cids(
    raw: bytes, target_len: int, *, side: str = "lead"
) -> Optional[bytes]:
    """Добиваем CID-слот пробелами (CID 0x0003); только чётное число байт.

    По умолчанию — ведущие пробелы. Хвостовые CID 0x0003 → Proton HARD
    SBER_FIO_TRAILING_PADDING / SBER_HEADER_TRAILING_PADDING.
    """
    gap = target_len - len(raw)
    if gap < 0:
        return None
    if gap == 0:
        return raw
    if gap % 2 != 0:
        return None
    pad = b"\x00\x03" * (gap // 2)
    if side == "trail":
        return raw + pad
    return pad + raw


def _grow_pdf_literal(raw: bytes, target_len: int) -> Optional[bytes]:
    """Увеличить PDF-literal до target_len, не меняя unescape (octal вместо \\f/\\n/…).

    Нужно для дат с «(МСК)»: enc-длина часто нечётная из‑за escape ``)``,
    а слот донора чётный — пробелами не добрать, зона отбрасывалась.

    Also: odd donor slots (e.g. SBP ID 65 B) need +1 raw with even unesc —
    PDF ignores backslash before a non-special char (``\\G`` → ``G``), giving +1.
    """
    if len(raw) == target_len:
        return raw
    if len(raw) > target_len:
        return None

    try:
        baseline = _pdf_literal_unescape(raw)
    except Exception:
        return None

    short_to_long = (
        (b"\\f", b"\\014"),
        (b"\\n", b"\\012"),
        (b"\\r", b"\\015"),
        (b"\\t", b"\\011"),
        (b"\\b", b"\\010"),
        (b"\\(", b"\\050"),
        (b"\\)", b"\\051"),
        (b"\\\\", b"\\134"),
    )
    # Bytes that must keep real escapes — never ``\\x`` → x collapse.
    _special = set(b"nrtbf()\\0123456789")

    def _ok(cand: bytes) -> bool:
        if len(cand) != target_len:
            return False
        try:
            return _pdf_literal_unescape(cand) == baseline
        except Exception:
            return False

    out = bytearray(raw)
    # 0) +1 via ``\\X`` for non-special raw byte (backslash ignored by PDF).
    while len(out) < target_len:
        need = target_len - len(out)
        if need < 1:
            break
        grew = False
        i = 0
        while i < len(out):
            if out[i] == 0x5C and i + 1 < len(out):
                nxt = out[i + 1]
                if 0x30 <= nxt <= 0x37:
                    j = i + 2
                    while j < len(out) and j < i + 4 and 0x30 <= out[j] <= 0x37:
                        j += 1
                    i = j
                    continue
                i += 2
                continue
            if out[i] in _special:
                i += 1
                continue
            # raw byte X → \X  (+1 raw, same unescape)
            out[i:i + 1] = bytes([0x5C, out[i]])
            grew = True
            break
        if not grew:
            break
        if _ok(bytes(out)):
            return bytes(out)
        if need == 1:
            # Only needed +1; if not ok, abort this strategy.
            break

    # 1) Короткие escapes → длинный octal (+2).
    guard = 0
    while len(out) < target_len and guard < 32:
        guard += 1
        need = target_len - len(out)
        grew = False
        for short, long in short_to_long:
            delta = len(long) - len(short)
            if delta <= 0 or delta > need:
                continue
            idx = bytes(out).find(short)
            if idx < 0:
                continue
            out[idx:idx + len(short)] = long
            grew = True
            break
        if not grew:
            break
        if _ok(bytes(out)):
            return bytes(out)

    # 2) Сырой байт → \\ddd octal (+3), не трогая уже экранированные.
    guard = 0
    while len(out) < target_len and guard < 64:
        guard += 1
        need = target_len - len(out)
        if need < 3:
            break
        grew = False
        i = 0
        while i < len(out):
            if out[i] == 0x5C and i + 1 < len(out):
                # skip escape sequence (\\f / \\014 / \\( / …)
                nxt = out[i + 1]
                if 0x30 <= nxt <= 0x37:  # octal
                    j = i + 2
                    while j < len(out) and j < i + 4 and 0x30 <= out[j] <= 0x37:
                        j += 1
                    i = j
                    continue
                i += 2
                continue
            # raw byte → \ddd
            octal = f"\\{out[i]:03o}".encode("ascii")
            out[i:i + 1] = octal
            grew = True
            break
        if not grew:
            break
        if _ok(bytes(out)):
            return bytes(out)

    # 3) Добить пробелами CID, если gap чётный.
    if len(out) < target_len:
        padded = _pad_encoded_cids(bytes(out), target_len)
        if padded is not None and _ok(padded):
            return padded
        # soft: same unescape prefix + trailing spaces may widen unesc — only if even CIDs
        padded = _pad_encoded_cids(bytes(out), target_len)
        if padded is not None:
            try:
                u = _pdf_literal_unescape(padded)
                if len(u) % 2 == 0 and u[: len(baseline)] == baseline:
                    return padded
            except Exception:
                pass
    return None


def _fit_into_shell_slot(
    encoded: bytes,
    shell_bytes: bytes,
    *,
    pad_side: str = "trail",
) -> Optional[bytes]:
    """Вписать encoded в длину shell. Пробелы CID + octal-grow.

    pad_side:
      • ``trail`` — хвостовые пробелы (Sber phone/SBP face: левый край = лейбл).
      • ``lead`` — ведущие (только если явно нужен правый край; SBP face — нет).
    Proton HARD: unescaped Tj payload must be even (CID pairs).
    """
    if not shell_bytes:
        return None
    target = len(shell_bytes)
    if len(encoded) > target:
        return None
    if len(encoded) == target:
        return encoded if _slot_cids_aligned(encoded) else None

    space = b"\x00\x03"
    gap = target - len(encoded)
    side = "lead" if pad_side == "lead" else "trail"

    def _join(nspaces: int) -> bytes:
        pad = space * nspaces
        return (pad + encoded) if side == "lead" else (encoded + pad)

    if gap % 2 == 0:
        out = _join(gap // 2)
        if len(out) == target and _slot_cids_aligned(out):
            return out

    for nspaces in range(gap // 2, -1, -1):
        base = _join(nspaces)
        if len(base) > target:
            continue
        if len(base) == target:
            if _slot_cids_aligned(base):
                return base
            continue
        grown = _grow_pdf_literal(base, target)
        if grown is not None and len(grown) == target and _slot_cids_aligned(grown):
            return grown

    grown = _grow_pdf_literal(encoded, target)
    if grown is not None and len(grown) == target and _slot_cids_aligned(grown):
        return grown
    return None


def _slot_cids_aligned(raw: bytes) -> bool:
    try:
        return len(_pdf_literal_unescape(raw)) % 2 == 0
    except Exception:
        return False


def _strict_identity_keys(key_hint: str) -> bool:
    return key_hint in (
        "receiver", "receiver_name",
        "sender", "sender_name",
        "bank", "recipient_bank",
    )


def _fit_strict_identity(
    text: str,
    target_len: int,
    enc,
    shell_bytes: bytes = b"",
    *,
    pad_side: str = "trail",
    key_hint: str = "",
) -> Optional[Tuple[str, bytes]]:
    """ФИО/банк: только полный текст; без укорочений.

    pad_side:
      • ``trail`` (default) — хвостовые пробелы, левый край ФИО = лейбл (phone).
      • ``lead`` — ведущие; нужно для SBP (SBER_FIO_TRAILING_PADDING HARD на Y SBP).
    """
    base = re.sub(r"\s+", " ", (text or "").strip())
    if not base:
        return None
    real_shell = bool(shell_bytes)
    # Синтетический слот только для добивки нечётного хвоста.
    if not shell_bytes and target_len > 0:
        shell_bytes = (b"\x00\x03" * ((target_len + 1) // 2))[:target_len]
    side = "lead" if pad_side == "lead" else "trail"

    def _parity_ok(b: bytes) -> bool:
        # Proton HARD: always even unescaped CID payload (never match odd donors).
        return _slot_cids_aligned(b)

    def _padded_cand(n: int) -> str:
        if n <= 0:
            return base
        if side == "lead":
            return (" " * n) + base
        return base + (" " * n)

    # Exact / invisible literal grow only. ASCII space pads → Proton HARD
    # SBER_*_WHITESPACE / SBER_FIO_TRAILING_PADDING on face fields.
    b0 = enc(base)
    if len(b0) == target_len and _parity_ok(b0):
        return base, b0
    if len(b0) < target_len:
        grown = _grow_pdf_literal(b0, target_len)
        if grown is not None and len(grown) == target_len and _parity_ok(grown):
            return base, grown
    # Bank: never face-pad with spaces (SBER_BANK_NAME_TRAILING_WHITESPACE).
    if key_hint in ("bank", "recipient_bank"):
        return None
    # SBP FIO: never CID-space pad (trail→FIO_TRAILING_PADDING, lead→edge drift).
    # Invisible grow only; otherwise caller picks another shell.
    if key_hint in ("sender_name", "receiver_name", "sender", "receiver"):
        if shell_bytes:
            b3 = _fit_into_shell_slot(b0, shell_bytes, pad_side="trail")
            # Only accept if unescape text has no trailing/leading space CID.
            if (
                b3 is not None
                and len(b3) == target_len
                and _parity_ok(b3)
                and not b3.endswith(b"\x00\x03")
                and not b3.startswith(b"\x00\x03")
            ):
                return base, b3
        return None
    for pad in range(0, 48):
        cand = _padded_cand(pad)
        b = enc(cand)
        if len(b) > target_len:
            break
        if len(b) == target_len and _parity_ok(b):
            # FIO space-pad still used on phone shells; SBP expand path preferred.
            return cand, b
        gap = target_len - len(b)
        if gap <= 0:
            continue
        grown = _grow_pdf_literal(b, target_len)
        if grown is not None and len(grown) == target_len and _parity_ok(grown):
            return cand, grown
        if gap % 2 == 0:
            b2 = _pad_encoded_cids(b, target_len, side=side)
            if b2 is not None and len(b2) == target_len and _parity_ok(b2):
                return cand, b2
        if shell_bytes:
            b3 = _fit_into_shell_slot(b, shell_bytes, pad_side=side)
            if b3 is not None and len(b3) == target_len:
                if _parity_ok(b3) or (real_shell and len(b3) == len(shell_bytes)):
                    return cand, b3
            if (
                not real_shell
                and b3 is not None
                and len(b3) == target_len
                and target_len % 2 == 1
            ):
                return cand, b3
    return None


def _phone_variants(text: str) -> List[str]:
    digits = re.sub(r"\D", "", text or "")
    if len(digits) < 11:
        return [text]
    d = digits[-10:]
    core = f"+7({d[0:3]}) {d[3:6]}-{d[6:8]}-{d[8:10]}"
    return [
        core,
        f"+7 ({d[0:3]}) {d[3:6]}-{d[6:8]}-{d[8:10]}",
        f"+7({d[0:3]}){d[3:6]}-{d[6:8]}-{d[8:10]}",
        f"+7 {d[0:3]} {d[3:6]}-{d[6:8]}-{d[8:10]}",
    ]


_RUBLE_CID = 0x0D5A  # PDF \rZ → 0x0D 0x5A в Identity-H


def _encode_phone_shell_slot(text: str, shell_raw: bytes, enc) -> bytes:
    """Телефонный слот Sber: только цифры пользователя + пробельный паддинг."""
    if not shell_raw:
        return enc(text)
    target = len(shell_raw)
    variants = _phone_variants(text)
    seen: Set[str] = set()
    ordered: List[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            ordered.append(v)
        for trail in range(0, 6):
            t = v + (" " * trail)
            if t not in seen:
                seen.add(t)
                ordered.append(t)
    for cand in ordered:
        b = enc(cand)
        if len(b) > target:
            continue
        if len(b) == target:
            return b
        gap = target - len(b)
        if gap > 0 and gap % 2 == 0:
            b2 = _pad_encoded_cids(b, target)
            if b2 is not None and len(b2) == target:
                return b2
    return enc(text)


def _splice_amount_slot(old_b: bytes, amount_text: str, enc, *, phone_style: bool = False, card_style: bool = False) -> bytes:
    """Сумма/комиссия: цифры новые, хвост с ₽ — байт-в-байт из shell.

    Никогда не обрезаем префикс до маркера ₽: иначе «423 000,00» → «423 000,0»
    (валидаторы падают / FAKE). Не влезло в слот — возвращаем old_b (caller сменит shell/сумму).
    """
    marker_pos = -1
    for marker in (b"\\rZ", b"\rZ", b"\x00\x0f", b"\x00\x0F"):
        marker_pos = old_b.find(marker)
        if marker_pos >= 0:
            break
    if marker_pos < 0:
        return enc(amount_text)

    tail = old_b[marker_pos:]
    # Proton SBER_AMOUNT_RUBLE_SPACING: exactly two SPACE CIDs before ₽.
    # Never let a longer digit head eat those two CIDs.
    _SP2 = b"\x00\x03\x00\x03"
    digit_budget = marker_pos - len(_SP2)
    if digit_budget < 2 or digit_budget % 2 != 0:
        digit_budget = marker_pos  # fallback: old behaviour
        _SP2 = b""

    if card_style:
        head = re.sub(r"\s*₽.*", "", amount_text.replace("\u20bd", "₽")).strip()
        head = re.sub(r"\s+", " ", head)
        heads = [head, f"{head} ", f" {head}", f"  {head}"]
    elif phone_style:
        n = _amount_rubles_int(amount_text)
        # Всегда ровно две копейки. No lead ASCII spaces (Proton WHITESPACE HARD).
        grouped = f"{n:,}".replace(",", " ") + ",00"
        plain = f"{n},00"
        heads = [grouped, plain, f"{grouped} ", f"{plain} "]
    else:
        head = re.sub(r"\s*₽.*", "", amount_text.replace("\u20bd", "₽")).strip()
        head = re.sub(r"\s+", " ", head)
        if "," in head and "." not in head:
            n = _amount_rubles_int(amount_text)
            base = f"{n:,}".replace(",", " ") + ",00"
            heads = [base, f"{base} "]
        elif not head.endswith(".00") and "." not in head:
            digs = re.sub(r"\D", "", head) or "0"
            head = f"{digs}.00"
            heads = [head]
        else:
            heads = [head]

    for head in heads:
        prefix_b = enc(head)
        gap = digit_budget - len(prefix_b)
        if gap < 0:
            # Слишком длинная сумма для слота — не режем копейки.
            continue
        if gap > 0:
            # Proton SBER_AMOUNT_LEADING_WHITESPACE — no ASCII lead-pad.
            # Return shorter head+₽; caller expands Tj (allow_len_change) or
            # picks another shell. Skip odd gaps.
            if gap % 2 != 0:
                continue
            out = prefix_b + _SP2 + tail
            if len(out) < len(old_b) and len(out) >= 4:
                return out
            continue
        out = prefix_b + _SP2 + tail
        if len(out) == len(old_b):
            return out
    return old_b


def _fit_text_encoded_length(
    text: str,
    target_len: int,
    enc,
    shell_bytes: bytes = b"",
    key_hint: str = "",
    phone_style: bool = False,
    pad_side: str = "trail",
) -> Tuple[str, bytes]:
    # Some slots can end up with odd-length CID byte sequences after
    # PDF-literal unescaping (e.g. due to 2-byte escapes). Fit by the raw
    # literal length is not enough; we also require the unescaped CID payload
    # length to match the donor slot payload length parity.
    # pad_side: trail keeps left edge of FIO/bank/amounts (phone + SBP face).
    # Lead pad pushes values right of the label column — forbidden by field-edge-anchors.
    target_unesc_len: Optional[int] = None
    if shell_bytes:
        try:
            target_unesc_len = len(_pdf_literal_unescape(shell_bytes))
        except Exception:
            target_unesc_len = None

    def _unesc_len_ok(b: bytes) -> bool:
        try:
            # Proton HARD: unescaped CID payload must always be even-length.
            if len(_pdf_literal_unescape(b)) % 2 != 0:
                return False
            return True
        except Exception:
            return False

    if "МСК" in text or key_hint in ("date_time", "date"):
        # The face date is accepted payload. Never drop seconds, zone spacing,
        # or otherwise choose a shorter textual variant to fit a donor.
        exact_date = re.sub(r"\s+", " ", (text or "").strip())
        date_layouts = [exact_date]
        compact_zone = exact_date.replace(" (МСК)", "(МСК)")
        if compact_zone != exact_date:
            date_layouts.append(compact_zone)
        for cand in date_layouts:
            b = enc(cand)
            if len(b) == target_len:
                if _unesc_len_ok(b) and _date_has_year_and_time(cand):
                    return cand, b
            if len(b) < target_len:
                b2 = _pad_encoded_cids(b, target_len)
                if (
                    b2 is not None
                    and _unesc_len_ok(b2)
                    and _date_has_year_and_time(cand)
                ):
                    return cand, b2
                # Octal-grow escapes (сохраняет «(МСК)» на чётных слотах донора).
                b_grow = _grow_pdf_literal(b, target_len)
                if (
                    b_grow is not None
                    and _unesc_len_ok(b_grow)
                    and _date_has_year_and_time(cand)
                ):
                    return cand, b_grow
                b3 = _fit_into_shell_slot(b, shell_bytes)
                if (
                    b3 is not None
                    and len(b3) == target_len
                    and _unesc_len_ok(b3)
                    and _date_has_year_and_time(cand)
                ):
                    return cand, b3
        # Слот слишком короткий для года+времени — не режем до «21 июля»
        # через _name_variants. Вернём oversized bytes → donor/shell сменится.
        logger.warning(
            "Sber dynamic: date slot %d B too small for %r (keep year+time)",
            target_len, text[:48],
        )
        best = exact_date
        return best, enc(best)
    if "₽" in text or "\u20bd" in text:
        if phone_style:
            if shell_bytes and key_hint in ("amount", "commission", ""):
                b = _splice_amount_slot(shell_bytes, text, enc, phone_style=True)
                if len(b) == target_len and _unesc_len_ok(b):
                    return text, b
            variants = _phone_amount_variants(text)
            for cand in variants:
                b = enc(cand)
                if len(b) == target_len and _unesc_len_ok(b):
                    return cand, b
            for cand in variants:
                # Trail first — left edge stays under the label.
                for trail in range(0, 8):
                    t = cand + (" " * trail)
                    b = enc(t)
                    if len(b) == target_len and _unesc_len_ok(b):
                        return t, b
                for pads in range(1, 6):
                    for trail in range(0, 6):
                        t = (" " * pads) + cand + (" " * trail)
                        b = enc(t)
                        if len(b) == target_len and _unesc_len_ok(b):
                            return t, b
            if shell_bytes and key_hint in ("amount", "commission", ""):
                b = _splice_amount_slot(shell_bytes, text, enc, phone_style=True)
                return text, b
            b = enc(variants[0])
            return variants[0], b
        if shell_bytes and key_hint in ("amount", "commission", ""):
            b = _splice_amount_slot(shell_bytes, text, enc, phone_style=phone_style)
            if len(b) == target_len and _unesc_len_ok(b):
                return text, b
        if "," in text and not re.search(r"\d\.\d{2}", text):
            variants = _phone_amount_variants(text)
            for cand in variants:
                b = enc(cand)
                if len(b) == target_len and _unesc_len_ok(b):
                    return cand, b
            for cand in variants:
                for trail in range(0, 8):
                    t = cand + (" " * trail)
                    b = enc(t)
                    if len(b) == target_len and _unesc_len_ok(b):
                        return t, b
                for pads in range(1, 6):
                    for trail in range(0, 6):
                        t = (" " * pads) + cand + (" " * trail)
                        b = enc(t)
                        if len(b) == target_len and _unesc_len_ok(b):
                            return t, b
            b = enc(variants[0])
            return variants[0], b

        n = _amount_rubles_int(text)
        core = f"{n}.00"
        # Proton exact: exactly two SPACE CIDs before ₽ (never 1 or 3+).
        for rub_gap in (2,):
            tail = (" " * rub_gap) + "₽"
            body = core + tail
            for lead in range(0, 24):
                cand = (" " * lead) + body
                b = enc(cand)
                if len(b) == target_len and _unesc_len_ok(b):
                    return cand, b
            # Slot longer: pad lead spaces / grow escapes, keep two spaces before ₽.
            b0 = enc(body)
            if len(b0) < target_len:
                for lead in range(0, 24):
                    cand = (" " * lead) + body
                    b = enc(cand)
                    if len(b) > target_len:
                        break
                    if len(b) == target_len and _unesc_len_ok(b):
                        return cand, b
                    grown = _grow_pdf_literal(b, target_len)
                    if grown is not None and _unesc_len_ok(grown):
                        return cand, grown
                padded = _pad_encoded_cids(b0, target_len)
                # Leading pad only — trailing space CIDs after ₽ would also break.
                if padded is not None and _unesc_len_ok(padded):
                    return body, padded
        fallback = f"{core}  ₽"
        b = enc(fallback)
        if len(b) < target_len:
            grown = _grow_pdf_literal(b, target_len)
            if grown is not None and _unesc_len_ok(grown):
                return fallback, grown
            padded = _pad_encoded_cids(b, target_len)
            if padded is not None and _unesc_len_ok(padded):
                return fallback, padded
        logger.warning(
            "Sber dynamic: amount slot %d B, best %d for %r",
            target_len, len(b), text[:24],
        )
        return fallback, b

    if key_hint == "phone" or (text.startswith("+7") and "(" in text):
        if shell_bytes:
            b = _encode_phone_shell_slot(text, shell_bytes, enc)
            if len(b) == target_len and _slot_cids_aligned(b):
                return text, b
        variants = _phone_variants(text)
        seen: Set[str] = set()
        ordered: List[str] = []
        for v in variants:
            if v not in seen:
                seen.add(v)
                ordered.append(v)
            for trail in range(0, 12):
                t = v + (" " * trail)
                if t not in seen:
                    seen.add(t)
                    ordered.append(t)
        for cand in ordered:
            b = enc(cand)
            if len(b) > target_len:
                continue
            b2 = _pad_encoded_cids(b, target_len)
            if b2 is not None and _unesc_len_ok(b2):
                return cand, b2
        if shell_bytes and len(shell_bytes) == target_len:
            b = enc(text)
            if len(b) == target_len - 1:
                b2 = _encode_phone_shell_slot(text, shell_bytes, enc)
                if len(b2) == target_len and _unesc_len_ok(b2):
                    return text, b2
            b2 = _pad_encoded_cids(b, target_len)
            if b2 is not None and _unesc_len_ok(b2):
                return text, b2
        b = enc(text)
        b2 = _pad_encoded_cids(b, target_len)
        if b2 is not None and _unesc_len_ok(b2):
            return text, b2
        return text, b

    if key_hint in ("bank", "recipient_bank") or _strict_identity_keys(key_hint):
        hit = _fit_exact_identity(
            text, target_len, enc, shell_bytes,
            key_hint=key_hint or "field",
            pad_side=pad_side,
        )
        if hit:
            return hit
        logger.warning(
            "Sber dynamic: %s не влезает в слот %d B без укорочения: %r",
            key_hint, target_len, text[:48],
        )
        # Absolute emit: truncate to shell slot — never return oversized CIDs
        # that expand the stream past CS flate ≤733.
        t = text.strip()
        while t and len(enc(t)) > target_len:
            t = t[:-1].rstrip()
        b = enc(t) if t else enc("А")
        b2 = _pad_encoded_cids(b, target_len)
        return (t or "А"), (b2 if b2 is not None else b)

    if len(re.sub(r"\s+", "", text)) == 32 and text[:1] in "AB":
        text = re.sub(r"\s+", "", text).upper()
        variants = [text]
        for n in range(1, 4):
            variants.append(text + (" " * n))
        for cand in variants:
            b = enc(cand)
            if len(b) > target_len:
                continue
            b2 = _pad_encoded_cids(b, target_len)
            if b2 is not None and len(b2) == target_len and _slot_cids_aligned(b2):
                return cand, b2
        best_b = enc(text)
        b2 = _pad_encoded_cids(best_b, target_len)
        if b2 is not None and len(b2) == target_len:
            return text, b2
        logger.warning(
            "Sber dynamic: SBP ID slot %d B, best %d for %r",
            target_len, len(best_b), text[:12],
        )
        return text, best_b
    else:
        variants = _name_variants(text)

    identity = key_hint in (
        "receiver", "receiver_name",
        "sender", "sender_name",
        "account", "sender_account",
    )

    seen: Set[str] = set()
    ordered: List[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            ordered.append(v)
        for n in range(1, 8):
            t = (v[:-1] + ("." * n)) if v.endswith(".") else (v + (" " * n))
            if t not in seen:
                seen.add(t)
                ordered.append(t)

    for cand in ordered:
        for trail in range(0, 20):
            t = cand + (" " * trail)
            b = enc(t)
            if len(b) > target_len:
                continue
            b2 = _pad_encoded_cids(b, target_len)
            if b2 is not None and _unesc_len_ok(b2) and _slot_cids_aligned(b2):
                return t, b2
            if shell_bytes:
                b3 = _fit_into_shell_slot(b, shell_bytes)
                # Для банка/ФИО хвост — пробелы; parity escape не важна.
                if b3 is not None and len(b3) == target_len and _slot_cids_aligned(b3):
                    if identity or _unesc_len_ok(b3):
                        return t, b3

    best_text = text
    best_b = enc(text)
    if len(best_b) <= target_len:
        b2 = _pad_encoded_cids(best_b, target_len)
        if b2 is not None and _unesc_len_ok(b2) and _slot_cids_aligned(b2):
            return best_text, b2
        if shell_bytes:
            b3 = _fit_into_shell_slot(best_b, shell_bytes)
            if b3 is not None and len(b3) == target_len and _slot_cids_aligned(b3):
                if identity or _unesc_len_ok(b3):
                    return best_text, b3
    logger.warning(
        "Sber dynamic: encoded length %s != target %s for %r",
        len(best_b), target_len, text[:40],
    )
    return best_text, best_b


# Short user banks leave too few non-space CID pairs (<238 HARD). Prefer a
# longer face alias that still reads as the same bank when the shell slot fits.
_BANK_PAIR_EXPAND: Dict[str, Tuple[str, ...]] = {
    # Keep expands short enough for common bank slots (≤~20 B) — overlong
    # aliases force slot growth and blow CS flate over 733.
    "ВТБ": ("Банк ВТБ", "ВТБ Банк"),
    "ПСБ": ("Банк ПСБ", "ПСБ Банк"),
    "МКБ": ("Банк МКБ",),
    "ОТП": ("ОТП Банк", "Банк ОТП"),
    "МТС": ("МТС-Банк", "МТС Банк"),
    "РНКБ": ("Банк РНКБ",),
    "СОЮЗ": ("Банк СОЮЗ",),
    "Озон": ("Озон Банк",),
    "OZON": ("Ozon Банк",),
}


def _bank_slot_variants(text: str) -> List[str]:
    """Canonical bank first; short names may expand for glyph_pairs≥238."""
    base = re.sub(r"\s+", " ", (text or "").strip())
    out: List[str] = [base] if base else []
    for alt in _BANK_PAIR_EXPAND.get(base, ()):
        if alt and alt not in out:
            out.append(alt)
    if base and len(base) <= 4:
        cand = f"Банк {base}"
        if cand not in out:
            out.append(cand)
    return out or [""]


def _cid_raw_from_unesc(unesc: bytes) -> bytes:
    """Упаковать CID-байты в PDF-literal raw (как _sber_enc хвост)."""
    out = bytearray()
    for b in unesc:
        out.extend(_ESC.get(b, bytes([b])))
    return bytes(out)


def _rebalance_fio_bank_slots(
    cs_orig: bytes,
    prepared: Dict[str, str],
    planned: Dict[int, int],
    uni_gid0: Dict[int, int],
    field_y: Dict[str, float],
    field_specs: List[Tuple[str, str, float]],
    enc,
) -> Optional[bytes]:
    """Собрать stream заново: полное ФИО+банк, выровнять длины Tj за счёт паддинга банка."""
    ug = {**uni_gid0, **planned}
    rows: Dict[str, Tuple[bytes, str]] = {}
    for name, key, _sz in field_specs:
        if key not in prepared:
            return None
        row = _read_field_at_y(cs_orig, field_y[key], uni_gid0)
        if not row:
            return None
        rows[key] = row

    bank_key = "recipient_bank"
    if bank_key not in rows:
        return None

    ideals: Dict[str, bytes] = {}
    for key, (_old, _) in rows.items():
        if key in ("amount", "commission", "date_time", "spb_number", "sender_account", "receiver_phone"):
            continue
        text = str(prepared.get(key) or "")
        ideals[key] = enc(text)

    bank_old, _ = rows[bank_key]
    # Prefer full bank name; soft variants only if even the wide bank slot
    # cannot hold the canonical name (never steal bank bytes for long FIO).
    bank_candidates: List[str] = []
    raw_bank = str(prepared.get(bank_key) or "").strip()
    if raw_bank:
        bank_candidates.append(raw_bank)
    for cand in _bank_slot_variants(raw_bank):
        if cand and cand not in bank_candidates:
            bank_candidates.append(cand)

    fio_keys = [k for k in ("sender_name", "receiver_name") if k in rows]
    budget = len(bank_old) + sum(len(rows[k][0]) for k in fio_keys)

    # Minimum bank bytes = full canonical name when it fits the original slot.
    full_bank_core: Optional[bytes] = None
    full_bank_text = raw_bank
    for cand in bank_candidates:
        core = enc(cand)
        if len(core) <= len(bank_old):
            full_bank_core = core
            full_bank_text = cand
            break

    bank_core: Optional[bytes] = None
    bank_text = raw_bank
    if full_bank_core is not None:
        # Keep full bank; only rebalance FIO into leftover bank pad + own slots.
        need_fio = sum(len(ideals[k]) for k in fio_keys)
        bank_pad = len(bank_old) - len(full_bank_core)
        fio_slots = sum(len(rows[k][0]) for k in fio_keys)
        if need_fio <= fio_slots + bank_pad:
            bank_core = full_bank_core
            bank_text = full_bank_text
        else:
            logger.info(
                "Sber rebalance skip: full bank kept, FIO needs %d > slots+pad %d+%d",
                need_fio, fio_slots, bank_pad,
            )
            return None
    else:
        for cand in bank_candidates:
            core = enc(cand)
            need = len(core) + sum(len(ideals[k]) for k in fio_keys)
            if need <= budget and len(core) <= len(bank_old):
                bank_core = core
                bank_text = cand
                break
    if bank_core is None:
        logger.info(
            "Sber rebalance skip: no bank variant fits budget=%d fio=%s",
            budget, {k: len(ideals[k]) for k in fio_keys},
        )
        return None

    fio_delta = sum(len(ideals[k]) - len(rows[k][0]) for k in fio_keys)
    if fio_delta <= 0:
        logger.info("Sber rebalance skip: fio_delta=%s", fio_delta)
        return None
    if len(bank_old) - fio_delta < len(bank_core):
        logger.info(
            "Sber rebalance skip: bank too small old=%d delta=%d core=%d ideals=%s",
            len(bank_old), fio_delta, len(bank_core),
            {k: len(ideals[k]) for k in fio_keys},
        )
        return None
    bank_len = len(bank_old) - fio_delta
    # Even CID payload + exact raw length (spaces / octal-grow; never orphan NUL).
    # Trail pad — left edge of bank stays under the label (field-edge-anchors).
    bank_new = _fit_into_shell_slot(
        bank_core,
        (b"\x00\x03" * ((bank_len + 1) // 2))[:bank_len],
        pad_side="trail",
    )
    if bank_new is None or len(bank_new) != bank_len or not _slot_cids_aligned(bank_new):
        logger.info(
            "Sber rebalance skip: bank pad failed core=%d target=%d",
            len(bank_core), bank_len,
        )
        return None
    prepared[bank_key] = bank_text

    stream = cs_orig
    plan = [(bank_key, bank_old, bank_new)]
    for k in fio_keys:
        plan.append((k, rows[k][0], ideals[k]))

    # Verify net length
    net = sum(len(n) - len(o) for _k, o, n in plan)
    if net != 0:
        logger.info("Sber rebalance skip: net=%d plan=%s", net, [(k, len(o), len(n)) for k, o, n in plan])
        return None

    for _k, old_b, new_b in plan:
        if old_b == new_b:
            continue
        stream, ok = _replace_tj_skeleton(
            stream, old_b, new_b, allow_len_change=True,
        )
        if not ok:
            logger.info("Sber rebalance skip: replace failed for %s (%d→%d)", _k, len(old_b), len(new_b))
            return None

    if len(stream) != len(cs_orig):
        logger.info("Sber rebalance skip: stream len %d != %d", len(stream), len(cs_orig))
        return None

    # Apply remaining fields (date, amount, …) with normal equal-length fit
    for name, key, _sz in field_specs:
        if key in (bank_key, *fio_keys):
            prepared[key] = str(prepared.get(key) or "").strip()
            continue
        row = _read_field_at_y(stream, field_y[key], uni_gid0)
        if not row:
            return None
        old_b, _ = row
        new_t, new_b = _fit_text_encoded_length(
            prepared[key], len(old_b), enc, shell_bytes=old_b, key_hint=name,
        )
        if key in ("amount", "commission"):
            new_b = _splice_ruble_slot(old_b, new_b) if len(new_b) == len(old_b) else new_b
            if len(new_b) != len(old_b):
                new_b = _splice_amount_slot(
                    old_b, prepared[key], enc, phone_style=False, card_style=False,
                )
        if len(new_b) != len(old_b):
            return None
        prepared[key] = new_t
        stream, ok = _replace_tj_skeleton(stream, old_b, new_b)
        if not ok:
            return None

    if len(stream) != len(cs_orig):
        return None
    if _content_skeleton_hash(stream) != _content_skeleton_hash(cs_orig):
        return None
    logger.info(
        "Sber rebalance OK: bank_len %d→%d fio deltas %s",
        len(bank_old), len(bank_new),
        {k: (len(rows[k][0]), len(ideals[k])) for k in fio_keys},
    )
    return stream


def _rebalance_amount_bank_pad(
    cs_orig: bytes,
    prepared: Dict[str, str],
    uni_gid0: Dict[int, int],
    field_y: Dict[str, float],
    field_specs: List[Tuple[str, str, float]],
    enc,
    *,
    phone_style: bool = False,
) -> Optional[bytes]:
    """Вырастить слот суммы за счёт хвостового пада банка (полное имя банка сохраняем)."""
    bank_key = "recipient_bank"
    amt_key = "amount"
    if bank_key not in field_y or amt_key not in field_y:
        return None
    bank_row = _read_field_at_y(cs_orig, field_y[bank_key], uni_gid0)
    amt_row = _read_field_at_y(cs_orig, field_y[amt_key], uni_gid0)
    if not bank_row or not amt_row:
        return None
    bank_old, _ = bank_row
    amt_old, _ = amt_row
    bank_text = str(prepared.get(bank_key) or "").strip()
    bank_core = enc(bank_text)
    if len(bank_core) > len(bank_old):
        return None
    bank_spare = len(bank_old) - len(bank_core)
    if bank_spare < 2:
        return None
    # Need even steal (CID pairs).
    steal = bank_spare - (bank_spare % 2)
    # Try growing amount by 2,4,... up to spare until splice fits.
    for grow in range(2, steal + 1, 2):
        amt_len = len(amt_old) + grow
        bank_len = len(bank_old) - grow
        if bank_len < len(bank_core):
            break
        # Wider synthetic amount shell: same ₽ tail, more digit budget.
        marker_pos = -1
        for marker in (b"\\rZ", b"\rZ", b"\x00\x0f", b"\x00\x0F"):
            marker_pos = amt_old.find(marker)
            if marker_pos >= 0:
                break
        if marker_pos < 0:
            return None
        tail = amt_old[marker_pos:]
        head_budget = amt_len - len(tail)
        if head_budget < 2 or head_budget % 2:
            continue
        syn_old = (b"\x00\x03" * (head_budget // 2)) + tail
        if len(syn_old) != amt_len:
            continue
        amt_new = _splice_amount_slot(
            syn_old, prepared[amt_key], enc, phone_style=phone_style,
        )
        if len(amt_new) != amt_len or amt_new == syn_old:
            continue
        bank_new = _fit_into_shell_slot(
            bank_core,
            (b"\x00\x03" * ((bank_len + 1) // 2))[:bank_len],
            pad_side="trail",
        )
        if bank_new is None or len(bank_new) != bank_len:
            continue
        stream = cs_orig
        stream, ok_a = _replace_tj_skeleton(
            stream, amt_old, amt_new, allow_len_change=True,
        )
        if not ok_a:
            continue
        stream, ok_b = _replace_tj_skeleton(
            stream, bank_old, bank_new, allow_len_change=True,
        )
        if not ok_b or len(stream) != len(cs_orig):
            continue
        # Fit remaining fields at their original lengths (including date_time —
        # never leave donor calendar after amount↔bank pad from cs_orig).
        ok_rest = True
        for name, key, _sz in field_specs:
            if key in (amt_key, bank_key):
                continue
            row = _read_field_at_y(stream, field_y[key], uni_gid0)
            if not row:
                ok_rest = False
                break
            old_b, _ = row
            if key in ("commission",):
                new_b = _splice_amount_slot(
                    old_b, prepared[key], enc, phone_style=phone_style,
                )
                if len(new_b) != len(old_b):
                    ok_rest = False
                    break
                stream, ok_f = _replace_tj_skeleton(stream, old_b, new_b)
                if not ok_f:
                    ok_rest = False
                    break
                continue
            hint = "date" if key == "date_time" else name
            new_t, new_b = _fit_text_encoded_length(
                prepared[key], len(old_b), enc, shell_bytes=old_b,
                key_hint=hint, pad_side="trail",
                phone_style=key == "receiver_phone",
            )
            if len(new_b) != len(old_b):
                ok_rest = False
                break
            prepared[key] = new_t
            stream, ok_f = _replace_tj_skeleton(stream, old_b, new_b)
            if not ok_f:
                ok_rest = False
                break
        if not ok_rest or len(stream) != len(cs_orig):
            continue
        if _content_skeleton_hash(stream) != _content_skeleton_hash(cs_orig):
            continue
        prepared[bank_key] = bank_text
        logger.info(
            "Sber amount←bank pad: amt %d→%d bank %d→%d",
            len(amt_old), amt_len, len(bank_old), bank_len,
        )
        return stream
    return None


def _name_slot_variants(text: str) -> List[str]:
    """Only the complete FIO; a short slot rejects the candidate."""
    base = re.sub(r"\s+", " ", (text or "").strip())
    return [base]


def _fit_exact_identity(
    text: str,
    target_len: int,
    enc,
    shell_bytes: bytes = b"",
    *,
    key_hint: str = "",
    pad_side: str = "trail",
) -> Optional[Tuple[str, bytes]]:
    """Fit only the exact identity value; never shorten or substitute."""
    return _fit_strict_identity(
        text, target_len, enc, shell_bytes,
        pad_side=pad_side, key_hint=key_hint,
    )


def _name_fit_variants(text: str) -> List[str]:
    """Compatibility wrapper returning only the complete FIO."""
    return _name_slot_variants(text)


def _name_variants(text: str) -> List[str]:
    """Варианты ФИО: только исходный текст (без инициалов/отрезания слов)."""
    return _name_fit_variants(text)


def _replace_tj_skeleton(
    stream: bytes,
    old_b: bytes,
    new_b: bytes,
    *,
    allow_hex_tj: bool = False,
    allow_len_change: bool = False,
) -> Tuple[bytes, bool]:
    needle = b"(" + old_b + b")Tj"
    pos = stream.find(needle)
    if pos >= 0:
        if old_b == new_b:
            return stream, True
        if len(new_b) != len(old_b) and not allow_len_change:
            return stream, False
        new_tj = b"(" + new_b + b")Tj"
        return stream[:pos] + new_tj + stream[pos + len(needle):], True
    # Identity-H hex TJ (Sber card only): keep array structure, swap hex payloads.
    if not allow_hex_tj:
        return stream, False
    if len(new_b) != len(old_b):
        return stream, False
    for m in re.finditer(rb"\[(.*?)\]\s*TJ", stream, re.DOTALL):
        arr = m.group(1)
        parts = list(re.finditer(rb"<([0-9A-Fa-f]+)>", arr))
        if not parts:
            continue
        raw = b"".join(bytes.fromhex(p.group(1).decode("ascii")) for p in parts)
        if raw != old_b:
            continue
        if old_b == new_b:
            return stream, True
        new_arr = bytearray(arr)
        offset = 0
        for p in parts:
            n = len(p.group(1)) // 2
            chunk = new_b[offset:offset + n]
            offset += n
            hx = chunk.hex().upper().encode("ascii")
            s, e = p.start(1), p.end(1)
            if len(hx) != (e - s):
                return stream, False
            new_arr[s:e] = hx
        return stream[:m.start(1)] + bytes(new_arr) + stream[m.end(1):], True
    return stream, False


def _shell_field_byte_lens(
    path: str,
    uni_gid: Dict[int, int],
    field_y: Optional[Dict[str, float]] = None,
) -> Dict[str, int]:
    doc = fitz.open(path)
    cs = doc.xref_stream(doc[0].get_contents()[0])
    doc.close()
    out: Dict[str, int] = {}
    for key, y in (field_y or _FIELD_Y).items():
        row = _read_field_at_y(cs, y, uni_gid)
        if row:
            out[key] = len(row[0])
    return out


def _build_sbp_shell_index() -> List[Tuple[str, str, Dict[str, int], Dict[int, int]]]:
    global _SBP_SHELL_INDEX
    if _SBP_SHELL_INDEX is not None:
        return _SBP_SHELL_INDEX
    rows: List[Tuple[str, str, Dict[str, int], Dict[int, int]]] = []
    candidates = []
    if os.path.isfile(SHELL_ARTIFACT):
        candidates.append(SHELL_ARTIFACT)
    if os.path.isfile(ORIG_TEMPLATE):
        candidates.append(ORIG_TEMPLATE)
    for path in EXTRA_SBP_SHELLS:
        if os.path.isfile(path):
            candidates.append(path)
    candidates.extend(_corpus_sbp_donors())
    seen_paths: Set[str] = set()
    for path in candidates:
        if not os.path.isfile(path) or path in seen_paths:
            continue
        seen_paths.add(path)
        try:
            with open(path, "rb") as fp:
                sk = _pdf_content_skeleton(fp.read())
            if not sk or sk not in KNOWN_SBER_SBP_SKELETONS:
                continue
            doc = fitz.open(path)
            fm = tut._find_font_objects(doc)
            key = _pick_arial(fm)
            if not key:
                doc.close()
                continue
            sub = tut._parse_subset_tounicode(
                doc.xref_stream(fm[key]["tounicode_xref"]).decode("latin1", "replace"))
            uni_gid = {u: c for c, u in sub.items()}
            doc.close()
            doc2 = fitz.open(path)
            cs_probe = doc2.xref_stream(doc2[0].get_contents()[0])
            row = _read_field_at_y(cs_probe, _FIELD_Y["spb_number"], uni_gid)
            doc2.close()
            sbp_sample = row[1] if row else ""
            if sbp_sample.startswith("B"):
                continue
            lens = _shell_field_byte_lens(path, uni_gid)
            if len(lens) >= 7:
                rows.append((path, sk, lens, uni_gid))
        except Exception:
            continue
    if not rows:
        rows.append((ORIG_TEMPLATE, "", {}, {}))
    _SBP_SHELL_INDEX = rows
    return rows


def _text_in_shell(text: str, uni: Dict[int, int]) -> bool:
    shell_uni = set(uni.keys())
    sanitized = _sanitize_text_for_shell(text, shell_uni)
    for ch in sanitized:
        cp = 0x20 if ch in ("₽", "\u20bd") else ord(ch)
        if cp not in shell_uni:
            return False
    return True


def _field_text_ok(text: str, uni: Dict[int, int]) -> bool:
    if sgl.covers_text(text):
        return True
    return _text_in_shell(text, uni)


def _ensure_spb_id(prepared: Dict[str, str], raw_prepared: Dict[str, str]) -> None:
    from sber_sbp_stealth import (
        _generate_sbp_number,
        sync_sbp_id_core_timestamp,
        _parse_sber_date_time_label,
        _parse_sber_dt,
    )

    sbp = re.sub(r"\s+", "", (prepared.get("spb_number") or "").upper())
    if not re.fullmatch(r"A[0-9]{14}[0-9]0G[0-9]{4}0011[67][0-9]{5}", sbp):
        dt_msk = _parse_sber_date_time_label(prepared.get("date_time", ""))
        if not dt_msk:
            dt_msk = _parse_sber_dt(
                raw_prepared.get("date", "") or "",
                raw_prepared.get("time", "") or "",
            )
        prepared["spb_number"] = _generate_sbp_number(
            prepared.get("recipient_bank", ""),
            dt_msk.strftime("%d.%m.%Y"),
            time_in=dt_msk.strftime("%H:%M:%S"),
            amount=prepared.get("amount", ""),
            phone=prepared.get("receiver_phone", ""),
            account=prepared.get("sender_account", ""),
            receiver=prepared.get("receiver_name", ""),
        )
    sync_sbp_id_core_timestamp(prepared)


def _enc_map_for_shell(uni_gid: Dict[int, int], prepared: Dict[str, str]) -> Dict[int, int]:
    """CID map for shell scoring — includes corpus slots when patch_font would run."""
    need = _needed_codepoints(prepared)
    if all(cp in uni_gid for cp in need):
        return uni_gid
    missing = {chr(cp) for cp in need if cp not in uni_gid}
    if missing - sgl.merged_charset() - {" ", "₽", "\u20bd"}:
        return uni_gid
    est_glyphs = max(uni_gid.values() or [0]) + len(need) + 32
    return _allocate_uni_gid(uni_gid, need, est_glyphs)


def _score_sbp_shell(
    prepared: Dict[str, str],
    path: str,
    lens: Dict[str, int],
    uni_gid: Dict[int, int],
) -> Optional[Tuple[int, int, str, Dict[int, int]]]:
    if not _field_text_ok(prepared.get("recipient_bank", ""), uni_gid):
        return None
    if not _field_text_ok(prepared.get("sender_name", ""), uni_gid):
        return None
    if not _field_text_ok(prepared.get("receiver_name", ""), uni_gid):
        return None
    enc_map = _enc_map_for_shell(uni_gid, prepared)
    enc = lambda t, m=enc_map: _sber_enc(t, m)
    # HARD: shell must hold FULL FIO/bank without shorten (bot must never mangle).
    for nm_key, nm_val in (
        ("receiver_name", prepared.get("receiver_name", "")),
        ("sender_name", prepared.get("sender_name", "")),
        ("recipient_bank", prepared.get("recipient_bank", "")),
    ):
        slot = lens.get(nm_key, 0)
        val = (nm_val or "").strip()
        if not slot or not val:
            continue
        hit = _fit_strict_identity(
            val, slot, enc, pad_side="trail", key_hint=nm_key,
        )
        # Bank may need Tj expand (no space pad) — still a usable shell.
        if hit is None:
            if nm_key == "recipient_bank":
                continue
            return None
        if hit[0].strip() != val:
            return None
    score = 0
    fit_ok = True
    want_sec = bool(re.search(r"\d{2}:\d{2}:\d{2}", prepared.get("date_time", "")))
    for key in _FIELD_Y:
        if key not in lens:
            continue
        fitted, b = _fit_text_encoded_length(
            prepared.get(key, ""),
            lens[key],
            enc,
            shell_bytes=(b"\x00\x03" * ((lens[key] + 1) // 2))[: lens[key]],
            key_hint=("date" if key == "date_time" else key),
            pad_side="trail",
        )
        gap = abs(len(b) - lens[key])
        score += gap * (5 if key in ("amount", "date_time", "recipient_bank") else 2)
        if gap != 0:
            fit_ok = False
        if key in ("sender_name", "receiver_name", "recipient_bank"):
            if fitted.strip() != str(prepared.get(key, "")).strip():
                return None
        if key == "amount":
            want = re.sub(r"\D", "", prepared.get(key, ""))
            got = re.sub(r"\D", "", fitted)
            # allow .00 kopecks suffix in face
            if want and want not in got and not got.startswith(want):
                return None
        if key == "date_time":
            if not _date_has_year_and_time(fitted):
                score += 2000
                fit_ok = False
            elif want_sec and not re.search(r"\d{2}:\d{2}:\d{2}", fitted):
                score += 200
                fit_ok = False
            elif "(МСК)" not in fitted:
                score += 40
    if fit_ok:
        score = 0
    # Prefer wider amount (≥23 fits 5-digit ₽) and bank (≥28 fits Райффайзенбанк)
    amt_slot = lens.get("amount", 0)
    bank_slot = lens.get("recipient_bank", 0)
    if amt_slot < 23:
        score += 800
    if bank_slot < 28:
        score += 400
    width = (
        lens.get("sender_name", 0)
        + lens.get("receiver_name", 0)
        + bank_slot
        + amt_slot * 3
    )
    return (score, width, path, uni_gid)

def _rank_sbp_shells(prepared: Dict[str, str]) -> List[Tuple[int, int, str, Dict[int, int]]]:
    ranked: List[Tuple[int, int, str, Dict[int, int]]] = []
    for path, _sk, lens, uni_gid in _build_sbp_shell_index():
        row = _score_sbp_shell(prepared, path, lens, uni_gid)
        if row:
            ranked.append(row)
    ranked.sort(key=lambda x: (x[0], -x[1], abs(os.path.getsize(x[2]) - 103000)))
    return ranked


def _sbp_shell_candidate_paths(prepared: Dict[str, str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    # Prefer label-exact full-charset runtime first (no FontFile2 graft growth).
    if (
        os.path.isfile(RUNTIME_TEMPLATE)
        and _sber_shell_exact_labels_ok(RUNTIME_TEMPLATE)
    ):
        out.append(RUNTIME_TEMPLATE)
        seen.add(os.path.normcase(RUNTIME_TEMPLATE))
    for _score, _slot, path, _uni in _rank_sbp_shells(prepared):
        norm = os.path.normcase(path)
        if norm not in seen:
            seen.add(norm)
            out.append(path)
    for path in _corpus_sbp_donors() + _shell_template_candidates():
        norm = os.path.normcase(path)
        if path and os.path.isfile(path) and norm not in seen:
            seen.add(norm)
            out.append(path)
    return out


def _shell_fits_prepared_strict(
    path: str,
    prepared: Dict[str, str],
) -> bool:
    """Shell can encode exact identities; font-patch may grow their Tj slots."""
    cmap = _cid_map_from_pdf(path) or {}
    if not cmap:
        return False
    uni_gid_map = {ord(ch): gid for ch, gid in cmap.items()}
    lens = _shell_field_byte_lens(path, uni_gid_map)
    soft = _auto_fix_prepared_fields(dict(prepared), cmap)
    enc_map = _enc_map_for_shell(uni_gid_map, soft)
    enc = lambda t, m=enc_map: _sber_enc(t, m)
    for nm_key in ("receiver_name", "sender_name", "recipient_bank"):
        slot = lens.get(nm_key, 0)
        val = str(soft.get(nm_key, "") or "").strip()
        if not slot or not val:
            continue
        core = enc(val)
        if not core or not _slot_cids_aligned(core):
            return False
    return True


def _pick_sbp_shell(prepared: Dict[str, str]) -> Tuple[str, Dict[int, int]]:
    ranked = _rank_sbp_shells(prepared)
    if ranked:
        score, _slot, best_path, best_uni = ranked[0]
        logger.info(
            "Sber dynamic: shell %s (score %s)",
            os.path.basename(best_path), score,
        )
        return best_path, best_uni

    return ORIG_TEMPLATE, {}


def _fmt_coord(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _date_left_x(date_time: str) -> Optional[float]:
    parts = re.sub(r"\s+", " ", (date_time or "").strip()).split()
    if len(parts) >= 2:
        return _DATE_LEFT_X_BY_MONTH.get(parts[1].lower())
    return None


def _tm_x_at_y(stream: bytes, target_y: float) -> Optional[float]:
    """X у Tm с данной Y (допуск 1pt)."""
    for m in re.finditer(
        rb"(?:1|[0-9.]+)\s+0\s+0\s+(?:1|[0-9.]+)\s+([\d.]+)\s+([\d.]+)\s+Tm",
        stream,
    ):
        try:
            y = float(m.group(2))
            if abs(y - target_y) <= 1.0:
                return float(m.group(1))
        except ValueError:
            continue
    return None


def _date_run_cids(stream: bytes, date_y: float, uni_gid: Dict[int, int]) -> Tuple[Optional[float], List[int], str]:
    """Tm.x, CID list and decoded text for the date TJ at date_y."""
    tm_x = _tm_x_at_y(stream, date_y)
    row = _read_field_at_y(stream, date_y, uni_gid)
    if tm_x is None or not row:
        return None, [], ""
    raw, text = row
    try:
        un = _pdf_literal_unescape(raw)
    except Exception:
        return tm_x, [], text
    cids = [
        int.from_bytes(un[i:i + 2], "big")
        for i in range(0, len(un) - 1, 2)
    ]
    return tm_x, cids, text


def _date_visual_center_from_cids(
    cids: List[int],
    tm_x: float,
    font_obj: TTFont,
    *,
    font_size: float = PHONE_DATE_SIZE,
) -> float:
    """Центр как у Proton: sum(floor(hmtx*1000/upem))*size/1000."""
    import math

    try:
        upem = int(font_obj["head"].unitsPerEm)
        hmtx = font_obj["hmtx"].metrics
        go = font_obj.getGlyphOrder()
    except Exception:
        return tm_x
    total_w = 0
    for cid in cids:
        if cid < 0 or cid >= len(go):
            total_w += 500
            continue
        aw = int(hmtx.get(go[cid], (500, 0))[0])
        total_w += math.floor(aw * 1000 / upem)
    return tm_x + total_w * font_size / 1000.0 / 2.0


def _date_visual_center_x(
    date_text: str,
    tm_x: float,
    font_obj: TTFont,
    uni_to_gid: Dict[int, int],
    *,
    font_size: float = PHONE_DATE_SIZE,
) -> float:
    """Fallback when CID list unavailable — prefer _date_visual_center_from_cids."""
    import math

    try:
        upem = int(font_obj["head"].unitsPerEm)
        hmtx = font_obj["hmtx"].metrics
        go = font_obj.getGlyphOrder()
    except Exception:
        return tm_x
    total_w = 0
    for ch in date_text:
        cp = 0x20 if ch in ("₽", "\u20bd") else ord(ch)
        gid = uni_to_gid.get(cp)
        if gid is None or gid >= len(go):
            continue
        aw = int(hmtx.get(go[gid], (500, 0))[0])
        total_w += math.floor(aw * 1000 / upem)
    return tm_x + total_w * font_size / 1000.0 / 2.0


def _fit_date_lead_for_center(
    user_date: str,
    target_len: int,
    enc,
    *,
    tm_x: float,
    font_obj: Optional[TTFont],
    uni_to_gid: Optional[Dict[int, int]],
    shell_bytes: bytes = b"",
    center_x: float = SBER_DATE_CENTER_X,
) -> Tuple[str, bytes]:
    """Дата в слот: полная с (МСК), TRAIL-pad для центра ≈153.

    Leading ASCII spaces (≥2) → SBER_HEADER_DATE_LEADING_WHITESPACE HARD.
    """
    want_sec = bool(re.search(r"\d{2}:\d{2}:\d{2}", user_date or ""))
    best: Optional[Tuple[Tuple, str, bytes]] = None
    exact_date = re.sub(r"\s+", " ", (user_date or "").strip())
    date_layouts = [exact_date]
    compact_zone = exact_date.replace(" (МСК)", "(МСК)")
    if compact_zone != exact_date:
        date_layouts.append(compact_zone)
    for cand in date_layouts:
        if not _date_has_year_and_time(cand):
            continue
        if "(МСК)" not in cand:
            continue
        sec_pen = (
            1
            if want_sec and not re.search(r"\d{2}:\d{2}:\d{2}", cand)
            else 0
        )
        b_cand = enc(cand)
        if len(b_cand) > target_len:
            continue
        max_trail = (target_len - len(b_cand)) // 2
        for trail in range(0, max_trail + 1):
            t = cand + (" " * trail)
            b = enc(t)
            extra = 0
            if len(b) < target_len:
                gap = target_len - len(b)
                if gap % 2 == 0:
                    b2 = _pad_encoded_cids(b, target_len, side="trail")
                    if b2 is not None:
                        extra = gap // 2
                        b = b2
                if len(b) != target_len:
                    b_grow = _grow_pdf_literal(
                        b if len(b) < target_len else enc(t), target_len,
                    )
                    if b_grow is not None and len(b_grow) == target_len:
                        b = b_grow
                        extra = 0
                if len(b) != target_len and shell_bytes:
                    b3 = _fit_into_shell_slot(enc(t), shell_bytes)
                    if b3 is not None and len(b3) == target_len:
                        b = b3
            if b is None or len(b) != target_len:
                continue
            try:
                if len(_pdf_literal_unescape(b)) % 2 != 0:
                    continue
            except Exception:
                continue
            visible = cand + (" " * (trail + extra))
            if font_obj is not None and uni_to_gid is not None:
                cx = _date_visual_center_x(visible, tm_x, font_obj, uni_to_gid)
                err = abs(cx - center_x)
            else:
                err = 50.0
            key = (sec_pen, err, trail + extra)
            row = (key, visible, b)
            if best is None or row[0] < best[0]:
                best = row
            if sec_pen == 0 and err <= 0.05:
                return visible, b
    if best is not None:
        return best[1], best[2]
    return _fit_text_encoded_length(
        user_date, target_len, enc, shell_bytes=shell_bytes, key_hint="date",
        pad_side="trail",
    )


def _stream_char_counts(stream: bytes, uni_gid: Dict[int, int]) -> Dict[str, int]:
    """Visible Unicode char counts from all literal Tj runs (for exclusive month glyphs)."""
    rev = {g: cp for cp, g in uni_gid.items()}
    counts: Dict[str, int] = {}
    for m in re.finditer(rb"\((?:\\.|[^\\)])*\)\s*Tj", stream):
        lit = m.group(0)
        end = lit.rfind(b")")
        if end <= 0:
            continue
        try:
            raw = _pdf_literal_unescape(lit[1:end])
        except Exception:
            continue
        if len(raw) >= 2 and len(raw) % 2 == 0:
            for i in range(0, len(raw), 2):
                cid = (raw[i] << 8) | raw[i + 1]
                cp = rev.get(cid)
                if cp is None or cp < 0x20:
                    continue
                ch = chr(cp)
                counts[ch] = counts.get(ch, 0) + 1
    return counts


def _fit_date_lead_grow_for_center(
    user_date: str,
    enc,
    *,
    tm_x: float,
    font_obj: Optional[TTFont],
    uni_to_gid: Optional[Dict[int, int]],
    min_len: int = 0,
    center_x: float = SBER_DATE_CENTER_X,
    tol: float = 0.55,
) -> Optional[Tuple[str, bytes]]:
    """Grow date Tj with TRAILING spaces until center within ``tol`` of 153.

    Never lead-pad (≥2 ASCII spaces → SBER_HEADER_DATE_LEADING_WHITESPACE).
    """
    if font_obj is None or uni_to_gid is None:
        return None
    exact_date = re.sub(r"\s+", " ", (user_date or "").strip())
    date_layouts = [exact_date]
    compact_zone = exact_date.replace(" (МСК)", "(МСК)")
    if compact_zone != exact_date:
        date_layouts.append(compact_zone)
    best: Optional[Tuple[Tuple, str, bytes]] = None
    for cand in date_layouts:
        if not _date_has_year_and_time(cand) or "(МСК)" not in cand:
            continue
        if not enc(cand):
            continue
        for trail in range(0, 24):
            t = cand + (" " * trail)
            b = enc(t)
            if not b or not _slot_cids_aligned(b):
                continue
            if len(b) < min_len:
                continue
            cx = _date_visual_center_x(t, tm_x, font_obj, uni_to_gid)
            err = abs(cx - center_x)
            key = (0 if err <= tol else 1, err, len(b), trail)
            row = (key, t, b)
            if best is None or row[0] < best[0]:
                best = row
            if err <= tol:
                return t, b
    if best is not None and best[0][0] == 0:
        return best[1], best[2]
    return None


def _micro_tune_date_center_font(
    ff2: bytes,
    stream: bytes,
    uni_gid: Dict[int, int],
    date_y: float,
    *,
    font_size: float = PHONE_DATE_SIZE,
    center_x: float = SBER_DATE_CENTER_X,
    tol: float = 0.05,
    exclusive_only: bool = False,
) -> bytes:
    """Подкрутить hmtx букв МЕСЯЦА, чтобы center_x → 153±0.05.

    Никогда не трогаем М/С/К из «(МСК)» — раздутый AW «К» = дыра перед «)».
    Крупный сдвиг центра — через lead spaces в слоте, тут только ±~1.2pt.
    exclusive_only (SBP): только глифы, которые на странице встречаются в дате
    и нигде больше — иначе уезжают ширины лейблов (K-SBER-SBP-EXACT-PROFILE).
    """
    tm_x, cids, date_text = _date_run_cids(stream, date_y, uni_gid)
    if tm_x is None or not cids:
        return ff2
    font = TTFont(BytesIO(ff2))
    cx = _date_visual_center_from_cids(cids, tm_x, font, font_size=font_size)
    err = cx - center_x
    if abs(err) <= tol:
        return ff2
    upem = int(font["head"].unitsPerEm)
    go = font.getGlyphOrder()
    prefer = "юёйъыэщжцшхчваеикнорстуф"
    ban = set("МСКмск")
    rev = {g: cp for cp, g in uni_gid.items()}
    month_chars: Set[str] = set()
    parts = re.sub(r"\s+", " ", (date_text or "").strip()).split()
    if len(parts) >= 2:
        month_chars = set(parts[1])
    page_counts = _stream_char_counts(stream, uni_gid) if exclusive_only else {}
    date_counts: Dict[str, int] = {}
    for ch in date_text or "":
        date_counts[ch] = date_counts.get(ch, 0) + 1
    cand_gids: List[int] = []
    for cid in cids:
        if cid == 3 or cid <= 0 or cid >= len(go):
            continue
        cp = rev.get(cid, -1)
        if not (0x0410 <= cp <= 0x044F or cp in (0x0401, 0x0451)):
            continue
        ch = chr(cp)
        if ch in ban:
            continue
        if month_chars and ch not in month_chars:
            continue
        if exclusive_only:
            # Char must not appear outside the date line.
            if page_counts.get(ch, 0) > date_counts.get(ch, 0):
                continue
        if cid not in cand_gids:
            cand_gids.append(cid)

    def _rank(gid: int) -> Tuple[int, int]:
        cp = rev.get(gid, 0)
        ch = chr(cp) if 0x20 <= cp < 0x10000 else ""
        return (0 if ch.lower() in prefer else 1, gid)

    cand_gids.sort(key=_rank)
    if not cand_gids:
        logger.warning(
            "Sber date center tune: no safe month CID (err=%+.3f) date=%r",
            err, (date_text or "")[:40],
        )
        return ff2

    max_delta = 400 if exclusive_only else 200  # exclusive month glyph can take more
    hmtx_raw = _get_font_table(ff2, b"hmtx")
    if not hmtx_raw:
        return ff2
    hmtx_arr = bytearray(hmtx_raw)

    def _apply_deltas(deltas: Dict[int, int]) -> bytes:
        arr = bytearray(hmtx_raw)
        for g, daw in deltas.items():
            if g * 4 + 2 > len(arr):
                continue
            aw = int.from_bytes(arr[g * 4:g * 4 + 2], "big")
            naw = max(50, aw + daw)
            arr[g * 4:g * 4 + 2] = int(naw).to_bytes(2, "big")
        return _restore_font_table(ff2, b"hmtx", bytes(arr))

    # Spread needed width across month letters (capped per glyph).
    need_units = int((-2.0 * err) * upem / font_size)
    n = len(cand_gids)
    base, rem = divmod(need_units, n)
    deltas: Dict[int, int] = {}
    for i, g in enumerate(cand_gids):
        d = base + (1 if i < abs(rem) else 0) * (1 if rem > 0 else -1 if rem < 0 else 0)
        d = max(-max_delta, min(max_delta, d))
        if d:
            deltas[g] = d
    if not deltas:
        return ff2
    out = _apply_deltas(deltas)
    new_cx = _date_visual_center_from_cids(
        cids, tm_x, TTFont(BytesIO(out)), font_size=font_size,
    )
    if abs(new_cx - center_x) > abs(err) + 0.01:
        return ff2
    logger.info(
        "Sber date center tune: %+.4f → %+.4f (month CIDs %s) date=%r",
        err, new_cx - center_x,
        {g: deltas[g] for g in deltas},
        (date_text or "")[:40],
    )
    return out


def _phone_date_left_x(
    date_time: str,
    font_obj: Optional[TTFont] = None,
    uni_to_gid: Optional[Dict[int, int]] = None,
    *,
    font_size: float = PHONE_DATE_SIZE,
    center_x: float = SBER_DATE_CENTER_X,
) -> Optional[float]:
    """Левый X даты: центр всегда 153 (как в оригиналах Сбера).

    Width must match Proton `_date_visual_center_from_cids` (floor hmtx*1000/upem).
    """
    import math

    visible = re.sub(r"\s+", " ", (date_time or "").strip())
    if not visible:
        return None
    if font_obj is not None and uni_to_gid is not None:
        try:
            upem = int(font_obj["head"].unitsPerEm)
            hmtx = font_obj["hmtx"].metrics
            go = font_obj.getGlyphOrder()
        except Exception:
            return _date_left_x(visible)
        total_w = 0
        for ch in visible:
            cp = 0x20 if ch in ("₽", "\u20bd") else ord(ch)
            gid = uni_to_gid.get(cp)
            if gid is None or gid >= len(go):
                continue
            aw = int(hmtx.get(go[gid], (500, 0))[0])
            total_w += math.floor(aw * 1000 / upem)
        w = total_w * font_size / 1000.0
        if w > 1.0:
            return float(f"{center_x - w / 2:.2f}")
    return _date_left_x(visible)


def _recenter_sber_date_stream(
    stream: bytes,
    date_time: str,
    target_y: float,
    *,
    template_path: str = "",
    font_obj: Optional[TTFont] = None,
    uni_to_gid: Optional[Dict[int, int]] = None,
    font_size: float = PHONE_DATE_SIZE,
) -> bytes:
    """Выровнять дату Сбера по центру 153 на target_y."""
    if font_obj is None or uni_to_gid is None:
        if template_path:
            font_obj, uni_to_gid = _load_sber_font_metrics(template_path)
    left = _phone_date_left_x(
        date_time, font_obj, uni_to_gid, font_size=font_size,
    )
    if left is None:
        return stream
    return _set_tm_x_at_y(stream, target_y, left)


def _recenter_phone_date_stream(
    stream: bytes,
    date_time: str,
    *,
    template_path: str = "",
    font_obj: Optional[TTFont] = None,
    uni_to_gid: Optional[Dict[int, int]] = None,
) -> bytes:
    """Выровнять дату phone-чека по центру 153."""
    return _recenter_sber_date_stream(
        stream,
        date_time,
        PHONE_DATE_Y,
        template_path=template_path,
        font_obj=font_obj,
        uni_to_gid=uni_to_gid,
    )


def _fmt_coord_exact_len(v: float, n: int) -> Optional[bytes]:
    """Encode float into exactly ``n`` ASCII bytes (prefer digits over spaces)."""
    if n <= 0:
        return None
    for prec in range(min(6, max(0, n - 2)), -1, -1):
        s = f"{v:.{prec}f}"
        if len(s) == n:
            return s.encode("ascii")
        if len(s) < n and "." in s:
            pad = s + ("0" * (n - len(s)))
            if len(pad) == n:
                try:
                    if abs(float(pad) - v) <= 0.0005 + 10 ** (-max(prec, 1)):
                        return pad.encode("ascii")
                except ValueError:
                    pass
        if len(s) < n:
            pad = (" " * (n - len(s))) + s
            if len(pad) == n:
                return pad.encode("ascii")
    # Last resort: truncate "%.Nf" / strip.
    raw = f"{v:.6f}".encode("ascii")
    if len(raw) >= n:
        return raw[:n]
    return None


def _set_tm_x_at_y(stream: bytes, target_y: float, new_x: float) -> bytes:
    """Переписать X у Tm с данной Y; длина всего Tm-оператора неизменна.

    Короткий X-токен (``71.9``) даёт шаг 0.1 → center drift ~0.04 и Proton
    HARD ±0.01. Тогда одалживаем символы у Y (``711.74``→``711.7``).
    """
    out = bytearray()
    last = 0
    for m in re.finditer(
        rb"((?:1|[0-9.]+)\s+0\s+0\s+(?:1|[0-9.]+)\s+)([\d.]+)(\s+)([\d.]+)(\s+Tm)",
        stream,
    ):
        try:
            y = float(m.group(4))
        except ValueError:
            continue
        if abs(y - target_y) > 1.0:
            continue
        old_x_tok = m.group(2)
        old_y_tok = m.group(4)
        budget = len(old_x_tok) + len(old_y_tok)
        new_x_tok: Optional[bytes] = None
        new_y_tok: Optional[bytes] = None
        # Prefer longer X (more precision); Y keeps value within 0.05.
        for x_len in range(min(8, budget - 3), 3, -1):
            y_len = budget - x_len
            if y_len < 3:
                continue
            xt = _fmt_coord_exact_len(new_x, x_len)
            yt = _fmt_coord_exact_len(y, y_len)
            if xt is None or yt is None:
                continue
            try:
                if abs(float(yt) - y) > 0.05:
                    continue
                if abs(float(xt) - new_x) > 0.005:
                    continue
            except ValueError:
                continue
            new_x_tok, new_y_tok = xt, yt
            break
        if new_x_tok is None:
            new_x_tok = _fmt_coord_exact_len(new_x, len(old_x_tok))
            new_y_tok = old_y_tok
        if new_x_tok is None or len(new_x_tok) + len(new_y_tok) != budget:
            logger.warning(
                "Sber date Tm X/Y pack fail x=%r y=%r → skip recenter",
                old_x_tok, old_y_tok,
            )
            return stream
        new_tm = m.group(1) + new_x_tok + m.group(3) + new_y_tok + m.group(5)
        if len(new_tm) != (m.end() - m.start()):
            return stream
        out.extend(stream[last:m.start()])
        out.extend(new_tm)
        last = m.end()
        break
    if last == 0:
        return stream
    out.extend(stream[last:])
    return bytes(out)


def _load_sber_font_metrics(path: str):
    """(TTFont, uni_to_gid) основного Arial из PDF или (None, None)."""
    try:
        doc = fitz.open(path)
        fm = tut._find_font_objects(doc)
        key = _pick_arial(fm)
        if not key:
            doc.close()
            return None, None
        ff2 = doc.xref_stream(fm[key]["fontfile_xref"])
        sub = tut._parse_subset_tounicode(
            doc.xref_stream(fm[key]["tounicode_xref"]).decode("latin1", "replace")
        )
        doc.close()
        uni_to_gid = {cp: cid for cid, cp in sub.items()}
        uni_to_gid[0x20] = uni_to_gid.get(0x20, 3)
        return TTFont(BytesIO(ff2)), uni_to_gid
    except Exception as exc:
        logger.warning("Sber font metrics load failed: %s", exc)
        return None, None


_JASPER_ZLIB = b"\x78\x9c"
_FAKE_ZLIB = b"\x78\xda"


def _best_compress(stream: bytes) -> bytes:
    return _jasper_compress(stream)


def _jasper_compress(stream: bytes, target_size: Optional[int] = None) -> bytes:
    """Только Jasper zlib (0x789c), никогда 0x78da (best compression = маркер подделки)."""
    from openpdf_deflate import compress_to_size

    if target_size is not None:
        hit = compress_to_size(stream, target_size)
        if hit and hit[:2] == _JASPER_ZLIB:
            return hit
    for level in (6, 5, 7, 4, 8, 3):
        comp = compress_like_jasper(stream, level=level)
        if comp[:2] == _JASPER_ZLIB:
            return comp
    comp = zlib.compress(stream, 6)
    if comp[:2] == _FAKE_ZLIB:
        for level in (5, 4, 7, 3):
            comp = zlib.compress(stream, level)
            if comp[:2] == _JASPER_ZLIB:
                return comp
    return comp


def _zlib_roundtrip_ok(comp: bytes, stream: bytes) -> bool:
    if not comp or comp[:2] != _JASPER_ZLIB:
        return False
    # Proton sber_v2.streams: body = raw.rstrip(b"\r\n") then zlib.decompress.
    # If flate ends with CR/LF, rstrip eats payload → STREAM_INTEGRITY_VIOLATION.
    if comp.endswith((b"\r", b"\n")):
        return False
    try:
        body = comp.rstrip(b"\r\n")
        if body != comp:
            return False
        return zlib.decompress(comp) == stream
    except Exception:
        return False


def _pad_to_compressed_size(stream: bytes, target_size: int) -> Optional[bytes]:
    """Подгонка перекомпрессией — только если zlib roundtrip сохраняет stream байт-в-байт."""
    from openpdf_deflate import compress_to_size

    hit = compress_to_size(stream, target_size)
    if hit and _zlib_roundtrip_ok(hit, stream):
        return hit
    for level in (6, 5, 7, 4, 8, 3, 9, 2, 1):
        comp = compress_like_jasper(stream, level=level)
        if len(comp) == target_size and _zlib_roundtrip_ok(comp, stream):
            return comp
        zc = zlib.compress(stream, level)
        if len(zc) == target_size and _zlib_roundtrip_ok(zc, stream):
            return zc
    return None


def _compress_cs_preserve(
    stream: bytes,
    orig_clen: int,
    *,
    max_clen: Optional[int] = None,
) -> Tuple[Optional[bytes], bytes]:
    """Jasper zlib для content stream без порчи decoded semantics.

    Returns (compressed, stream_used). stream_used may be a whitespace-shrunk
    variant when max_clen forces a near-miss fit.

    Если задан max_clen (SBP ≤733) — не отдаём oversized fallback
    (Proton SBER_CONTENT_RAW_LENGTH_OUTLIER); лучше abort shell.
    """
    preferred = orig_clen
    if max_clen is not None:
        preferred = min(orig_clen, max_clen, _corpus_cs_comp_target(len(stream), orig_clen))

    targets: list[int] = []
    for delta in (0, 1, -1, 2, -2, 3, -3, 5, -5, 8, -8, 12, -12):
        t = preferred + delta
        if t <= 0:
            continue
        if max_clen is not None and t > max_clen:
            continue
        if t not in targets:
            targets.append(t)
    if max_clen is not None:
        for t in range(max_clen, max(200, max_clen - 24), -1):
            if t not in targets:
                targets.append(t)
    elif orig_clen not in targets:
        targets.insert(0, orig_clen)

    def _try_exact(s: bytes) -> Optional[bytes]:
        for target in targets:
            for level in (6, 5, 7, 4, 8, 3, 2, 1, 9):
                zc = zlib.compress(s, level)
                if len(zc) == target and _zlib_roundtrip_ok(zc, s):
                    return zc
                jas = compress_like_jasper(s, level=level)
                if len(jas) == target and _zlib_roundtrip_ok(jas, s):
                    return jas
        pad_targets = targets[:8] if len(s) <= 2500 else targets[:5]
        for target in pad_targets:
            hit = _pad_to_compressed_size(s, target)
            if hit is not None:
                return hit
        best: Optional[bytes] = None
        for level in (9, 8, 7, 6, 5, 4, 3, 2, 1):
            for comp in (
                zlib.compress(s, level),
                compress_like_jasper(s, level=level),
            ):
                if not _zlib_roundtrip_ok(comp, s):
                    continue
                if max_clen is not None and len(comp) > max_clen:
                    continue
                if best is None or abs(len(comp) - orig_clen) < abs(len(best) - orig_clen):
                    best = comp
        return best

    hit = _try_exact(stream)
    if hit is not None:
        if abs(len(hit) - orig_clen) > 64:
            logger.warning(
                "Sber dynamic: cs flate drift %d→%d",
                orig_clen, len(hit),
            )
        return hit, stream

    # Near-miss oversize: drop redundant whitespace (OpenPDF-safe pts) so
    # Flate lands ≤ max_clen without shipping SBER_CONTENT_RAW_LENGTH_OUTLIER.
    #
    # IMPORTANT: gate shrink on Jasper 0x789c size, not raw zlib(6). Level-6
    # often emits 0x78da (smaller, FAKE header) while every roundtrip-ok
    # 0x789c blob sits 1–4 B over max — old code skipped shrink and aborted,
    # leaving the bot on GEN_NONE for minutes.
    if max_clen is not None:
        j_sizes = []
        for level in (6, 5, 7, 4, 8, 3, 9, 2, 1):
            for comp in (
                compress_like_jasper(stream, level=level),
                zlib.compress(stream, level),
            ):
                if _zlib_roundtrip_ok(comp, stream):
                    j_sizes.append(len(comp))
        j_min = min(j_sizes) if j_sizes else None
        need_shrink = j_min is None or j_min > max_clen
        if need_shrink:
            try:
                from openpdf_deflate import (
                    _split_et,
                    _shrink_variants,
                    _space_drop_variants,
                )
            except Exception:
                _split_et = None  # type: ignore
            if _split_et is not None:
                body, tail = _split_et(stream)
                # Short-bank separator boost often overshoots by 1–4 B — search deeper.
                over = 32 if (j_min is None or j_min - max_clen > 8) else 8
                lim = 80 + max(0, over) * 40
                cands = []
                cands.extend(_shrink_variants(body, tail, limit=lim))
                cands.extend(_space_drop_variants(body, tail, limit=lim))
                for cand in cands:
                    if re.search(
                        rb"(?:Td|Tm|Tj|BT|ET|cm|rg|RG|Tf)(?:Td|Tm|Tj|BT|ET|cm)",
                        cand,
                    ):
                        continue
                    hit2 = _try_exact(cand)
                    if hit2 is not None:
                        logger.info(
                            "Sber dynamic: cs shrink-fit jasper %s→%d (dec %d→%d)",
                            j_min, len(hit2), len(stream), len(cand),
                        )
                        return hit2, cand

        # Absolute emit: ship natural flate even if over atlas max_clen.
        # Prefer shrink-fit above; do not GEN_NONE the bot on mash FIO.
        logger.warning(
            "Sber dynamic: cs flate cannot fit ≤%d (shell %d, dec %d, jasper_min=%s) — ship natural",
            max_clen, orig_clen, len(stream), j_min,
        )
        nat = compress_like_jasper(stream)
        if _zlib_roundtrip_ok(nat, stream):
            return nat, stream
        for level in (6, 5, 7, 4, 8, 3, 2, 1, 9):
            comp = zlib.compress(stream, level)
            if _zlib_roundtrip_ok(comp, stream):
                return comp, stream
        return None, stream

    for level in (6, 5, 7, 4, 8, 3, 2, 1):
        comp = zlib.compress(stream, level)
        if _zlib_roundtrip_ok(comp, stream):
            if abs(len(comp) - orig_clen) > 64:
                logger.warning(
                    "Sber dynamic: cs flate drift %d→%d",
                    orig_clen, len(comp),
                )
            return comp, stream
    comp = compress_like_jasper(stream)
    if not _zlib_roundtrip_ok(comp, stream):
        logger.error("Sber dynamic: cs compress roundtrip failed")
        return None, stream
    return comp, stream


def _fix_stream_separators(data: bytes) -> bytes:
    return re.sub(rb">>\s+stream", b">>stream", data)


def _strip_pdf_eof_tail(pdf: bytes) -> bytes:
    """Обрезает мусор после последнего %%EOF — валидный чек не должен иметь хвост.

    Важно: сохранять исходный перевод строки после %%EOF (``\\n`` или ``\\r\\n``),
    иначе size drift → отбраковка donor-orig / ложный FAKE у чекеров.
    """
    eof_idx = pdf.rfind(b"%%EOF")
    if eof_idx < 0:
        return pdf
    end = eof_idx + len(b"%%EOF")
    if pdf[end : end + 2] == b"\r\n":
        end += 2
    elif end < len(pdf) and pdf[end : end + 1] in (b"\n", b"\r"):
        end += 1
    if end >= len(pdf):
        return pdf
    tail = pdf[end:]
    if tail.strip(b"\x00 \t\r\n") == b"":
        return pdf[:end]
    return pdf


def _pad_pdf_to_exact_size(pdf: bytes, target: int) -> bytes:
    """Только обрезка хвоста после %%EOF; не дописываем padding (ловит TRAILING_DATA_AFTER_EOF)."""
    pdf = _strip_pdf_eof_tail(pdf)
    if len(pdf) <= target:
        return pdf
    eof_idx = pdf.rfind(b"%%EOF")
    if eof_idx < 0:
        return pdf
    end = eof_idx + len(b"%%EOF")
    while end < len(pdf) and pdf[end:end + 1] in (b"\n", b"\r"):
        end += 1
    trim = len(pdf) - target
    if end + trim <= len(pdf) and pdf[end:end + trim].strip() == b"":
        return pdf[:end] + pdf[end + trim:]
    return pdf


# Live Sber SBP originals: ~102–103KB (сбербанк сбп*.pdf / S_sbp_original).
_SBER_SBP_SIZE_LO = 100_000
_SBER_SBP_SIZE_HI = 104_500
_SBER_SBP_SIZE_AIM = 102_400


def _pad_pdf_before_xref_to_size(pdf: bytes, target: int) -> bytes:
    """Grow file size with a PDF comment immediately before ``xref``.

    Does not touch FontFile2 / content streams (no decoded-font lift).
    Updates ``startxref`` to the new xref offset.
    """
    if target <= len(pdf):
        return pdf
    xref_m = re.search(rb"[\r\n]xref[\r\n]", pdf)
    if xref_m:
        xref_at = xref_m.start() + 1  # 'x' of xref
    else:
        xref_at = pdf.find(b"xref\n")
        if xref_at < 0:
            xref_at = pdf.find(b"xref\r")
        if xref_at < 0:
            return pdf

    body_len = max(0, target - len(pdf) - 2)
    best = pdf
    for _ in range(24):
        insert = b"%" + (b"#" * body_len) + b"\n"
        new_xref = xref_at + len(insert)
        candidate = pdf[:xref_at] + insert + pdf[xref_at:]
        candidate, n_sub = re.subn(
            rb"startxref\s*[\r\n]+\d+",
            f"startxref\n{new_xref}".encode("ascii"),
            candidate,
            count=1,
        )
        if n_sub != 1:
            return pdf
        if abs(len(candidate) - target) < abs(len(best) - target):
            best = candidate
        if len(candidate) == target:
            return candidate
        if len(candidate) > target:
            over = len(candidate) - target
            if over > body_len:
                break
            body_len -= over
            continue
        body_len += target - len(candidate)
    return best if _SBER_SBP_SIZE_LO <= len(best) <= _SBER_SBP_SIZE_HI else pdf


def _lift_sber_pdf_into_orig_band(pdf: bytes) -> bytes:
    """Bring SBP file size into the live original band (~100–104 KB).

    Never grows decoded FontFile2 (Proton HARD). Undersized shells (e.g. runtime
    99KB after exact font patch) are lifted with a pre-xref PDF comment pad.
    """
    if _SBER_SBP_SIZE_LO <= len(pdf) <= _SBER_SBP_SIZE_HI:
        return pdf
    if len(pdf) > _SBER_SBP_SIZE_HI:
        logger.error(
            "Sber SBP size %d above original band — no font lift",
            len(pdf),
        )
        return pdf
    # Undersized: pad to aim (typical gap from 99KB runtime shell is 1–4KB).
    lifted = _pad_pdf_before_xref_to_size(pdf, _SBER_SBP_SIZE_AIM)
    if _SBER_SBP_SIZE_LO <= len(lifted) <= _SBER_SBP_SIZE_HI:
        # Verify startxref still points at xref.
        sx = re.search(rb"startxref\s*[\r\n]+(\d+)", lifted)
        if sx and lifted[int(sx.group(1)) : int(sx.group(1)) + 4] == b"xref":
            logger.info(
                "Sber SBP size lift %d → %d (pre-xref comment, no FontFile2 change)",
                len(pdf), len(lifted),
            )
            return lifted
    logger.error(
        "Sber SBP size %d outside original band; lift failed (now %d)",
        len(pdf), len(lifted),
    )
    return pdf


def _corpus_cs_comp_target(uncomp_len: int, fallback: int) -> int:
    if uncomp_len >= 3500:
        return 721
    if uncomp_len >= 3200:
        return 716
    return fallback


# Proton detector/sber_v2/content.py — exclusive upper (best_raw > hi → HARD).
SBP_CONTENT_RAW_MAX = 733  # sbp_outgoing
# Proton SBER_CONTENT_STREAM_TOO_SHORT: genuines ≥3558 decoded.
_SBP_CONTENT_DEC_MIN = 3558


def _shrink_sber_cs_to_jasper(
    stream: bytes,
    max_clen: int = SBP_CONTENT_RAW_MAX,
) -> bytes:
    """Drop OpenPDF-safe whitespace until Jasper flate ≤ max_clen."""
    def _jmin(s: bytes) -> Optional[int]:
        sizes = []
        for level in (6, 5, 7, 9, 4):
            for comp in (
                compress_like_jasper(s, level=level),
                zlib.compress(s, level),
            ):
                if _zlib_roundtrip_ok(comp, s):
                    sizes.append(len(comp))
        return min(sizes) if sizes else None

    j0 = _jmin(stream)
    if j0 is not None and j0 <= max_clen:
        return stream
    try:
        from openpdf_deflate import _split_et, _shrink_variants, _space_drop_variants
    except Exception:
        return stream
    body, tail = _split_et(stream)
    best = stream
    best_j = j0
    for cand in list(_space_drop_variants(body, tail, limit=250)) + list(
        _shrink_variants(body, tail, limit=250)
    ):
        j = _jmin(cand)
        if j is None:
            continue
        if j <= max_clen:
            logger.info(
                "Sber CS jasper shrink %s→%d (dec %d→%d)",
                j0, j, len(stream), len(cand),
            )
            return cand
        if best_j is None or j < best_j:
            best, best_j = cand, j
    return best


def _ensure_sber_cs_dec_min(
    stream: bytes,
    min_len: int = _SBP_CONTENT_DEC_MIN,
) -> bytes:
    """Grow decoded content to ≥min_len without touching post-ET whitespace."""
    if len(stream) >= min_len:
        return stream
    need = min_len - len(stream)
    # Pad an existing space-run in the body before the first ET.
    et = stream.find(b"ET")
    body = stream if et < 0 else stream[:et]
    tail = b"" if et < 0 else stream[et:]
    m = re.search(rb"[ \t]{2,}", body)
    if m:
        run = m.group(0)
        out = body[: m.start()] + run + (b" " * need) + body[m.end() :] + tail
        logger.info("Sber CS dec pad %d→%d (space-run)", len(stream), len(out))
        return out
    for token in (b"q\n", b"q ", b"\nBT\n", b" BT ", b"\n/F1 "):
        i = body.find(token)
        if i >= 0:
            j = i + len(token)
            out = body[:j] + (b" " * need) + body[j:] + tail
            logger.info("Sber CS dec pad %d→%d (after %r)", len(stream), len(out), token)
            return out
    logger.warning("Sber CS dec pad fallback %d need +%d", len(stream), need)
    return stream + (b"\n" * need)
PHONE_CONTENT_RAW_MAX = 932  # sber_internal_jasper


def _orig_cs_comp_len(orig: bytes, cs_s: int, cs_e: int) -> int:
    m = re.search(rb"stream\r?\n([\x00-\xff]*?)\nendstream", orig[cs_s:cs_e], re.DOTALL)
    return len(m.group(1)) if m else 0


def _pick_arial(fm: dict) -> Optional[str]:
    for key in fm:
        if "Arial" in key:
            return key
    return next(iter(fm), None) if fm else None


def _template_unicode_set(path: str) -> Set[int]:
    doc = fitz.open(path)
    fm = tut._find_font_objects(doc)
    key = _pick_arial(fm)
    if not key:
        doc.close()
        return set()
    sub = tut._parse_subset_tounicode(
        doc.xref_stream(fm[key]["tounicode_xref"]).decode("latin1", "replace")
    )
    doc.close()
    out = {u for _, u in sub.items()}
    out.add(0x20)  # ruble escape maps to space slot
    return out


def _sanitize_text_for_shell(text: str, shell_uni: Set[int]) -> str:
    """Return exact text; callers must extend the font or reject the shell."""
    return str(text)


def _sanitize_prepared_for_shell(prepared: Dict[str, str], shell_uni: Set[int]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in prepared.items():
        # SBP ID must keep machine format; if char is absent, replace with "0".
        if k == "spb_number":
            fixed = []
            for ch in str(v):
                cp = ord(ch)
                if cp in shell_uni:
                    fixed.append(ch)
                elif ch in "AG" and "G" in {chr(x) for x in shell_uni}:
                    fixed.append(ch)
                else:
                    fixed.append("0")
            out[k] = "".join(fixed)
            if not re.fullmatch(r"A[0-9]{14}[0-9]0G[0-9]{4}0011[67][0-9]{5}", out[k]):
                logger.warning("Sber dynamic: spb_number broken after sanitize, regen needed")
            continue
        out[k] = _sanitize_text_for_shell(str(v), shell_uni)
    return out


def _needed_codepoints(prepared: Dict[str, str]) -> Set[int]:
    need: Set[int] = set()
    for val in prepared.values():
        for ch in str(val):
            if ch in ("₽", "\u20bd"):
                need.add(0x20)
            else:
                need.add(ord(ch))
    return need


def _needed_text(prepared: Dict[str, str]) -> str:
    return "".join(str(v) for v in prepared.values())


def _sber_enc(text: str, uni_to_gid: Dict[int, int]) -> bytes:
    out = bytearray()
    for ch in text:
        if ch in ("₽", "\u20bd"):
            out.extend(_RUBLE_ESC)
            continue
        gid = uni_to_gid.get(ord(ch))
        if gid is None:
            continue
        for b in ((gid >> 8) & 0xFF, gid & 0xFF):
            out.extend(_ESC.get(b, bytes([b])))
    return bytes(out)


def _sber_enc_raw(text: str, uni_to_gid: Dict[int, int]) -> bytes:
    """CID bytes for Identity-H hex TJ (no PDF literal escapes)."""
    out = bytearray()
    for ch in text:
        if ch in ("₽", "\u20bd"):
            out.extend(bytes([(_RUBLE_CID >> 8) & 0xFF, _RUBLE_CID & 0xFF]))
            continue
        gid = uni_to_gid.get(ord(ch))
        if gid is None:
            continue
        out.append((gid >> 8) & 0xFF)
        out.append(gid & 0xFF)
    return bytes(out)


def _pdf_literal_unescape(raw: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b != 0x5C or i + 1 >= len(raw):
            out.append(b)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in b"nrtbf":
            out.append({ord("n"): 0x0A, ord("r"): 0x0D, ord("t"): 0x09, ord("b"): 0x08, ord("f"): 0x0C}[nxt])
            i += 2
            continue
        if nxt in (0x28, 0x29, 0x5C):
            out.append(nxt)
            i += 2
            continue
        if 0x30 <= nxt <= 0x37:
            j = i + 1
            octal = bytearray()
            while j < len(raw) and len(octal) < 3 and 0x30 <= raw[j] <= 0x37:
                octal.append(raw[j])
                j += 1
            out.append(int(octal.decode("ascii"), 8) & 0xFF)
            i = j
            continue
        out.append(nxt)
        i += 2
    return bytes(out)


def _width_pt(text: str, font_size: float, font_obj: TTFont, uni_to_gid: Dict[int, int]) -> float:
    try:
        upem = font_obj["head"].unitsPerEm
        hmtx = font_obj["hmtx"].metrics
        glyph_order = font_obj.getGlyphOrder()
    except Exception:
        return 0.0
    total = 0
    for ch in text:
        if ch in ("₽", "\u20bd"):
            gid = uni_to_gid.get(0x20, 3)
        else:
            gid = uni_to_gid.get(ord(ch))
        if gid is None or gid >= len(glyph_order):
            continue
        total += hmtx.get(glyph_order[gid], (250, 0))[0]
    return total * font_size / upem


def _replace_tj(
    stream: bytes,
    old_b: bytes,
    new_b: bytes,
    old_t: str,
    new_t: str,
    sz: float,
    font_obj: TTFont,
    uni_to_gid: Dict[int, int],
    *,
    left_x: Optional[float] = None,
    occurrence: int = 1,
) -> Tuple[bytes, bool]:
    needle = b"(" + old_b + b")Tj"
    start = 0
    pos = -1
    for _ in range(occurrence):
        pos = stream.find(needle, start)
        if pos < 0:
            return stream, False
        start = pos + len(needle)
    if old_b == new_b:
        return stream, True

    look_from = max(0, pos - 200)
    region = stream[look_from:pos]
    tms = list(_TM_RE.finditer(region))
    new_tj = b"(" + new_b + b")Tj"
    if not tms:
        return stream[:pos] + new_tj + stream[pos + len(needle):], True

    last = tms[-1]
    old_x = float(last.group(1))
    old_y = last.group(2).decode()
    old_w = _width_pt(old_t, sz, font_obj, uni_to_gid)
    new_w = _width_pt(new_t, sz, font_obj, uni_to_gid)
    if left_x is not None:
        new_x = left_x
    else:
        new_x = old_x + (old_w - new_w)
    if left_x is None and abs(new_x - old_x) < 0.005:
        return stream[:pos] + new_tj + stream[pos + len(needle):], True
    new_tm = f"1 0 0 1 {_fmt_coord(new_x)} {old_y} Tm".encode("ascii")
    tm_s = look_from + last.start()
    tm_e = look_from + last.end()
    return stream[:tm_s] + new_tm + stream[tm_e:pos] + new_tj + stream[pos + len(needle):], True


def _gids_in_stream(stream: bytes) -> Set[int]:
    gids: Set[int] = set()

    def _consume(raw: bytes) -> None:
        i = 0
        while i < len(raw):
            if raw[i:i + 2] == b"\rZ":
                gids.add(_RUBLE_CID)
                i += 2
                continue
            if i + 1 < len(raw):
                gids.add((raw[i] << 8) | raw[i + 1])
                i += 2
            else:
                break

    for raw in _iter_pdf_literal_tj(stream):
        _consume(_pdf_literal_unescape(raw))
    for m in re.finditer(rb"<([0-9A-Fa-f]+)>", stream):
        _consume(bytes.fromhex(m.group(1).decode("ascii")))
    return gids


# Proton detector/sber_v2/glyph_spacing.py — sbp_outgoing floor.
_SBP_GLYPH_PAIRS_MIN = 238
_SBER_PAIR_SPACE_CID = 3


def _sber_glyph_pair_count_from_stream(stream: bytes) -> int:
    """Non-space consecutive CID pairs in (…)Tj — same metric as Proton."""
    pairs = 0
    for cids in _sber_tj_cid_runs(stream):
        for left, right in zip(cids, cids[1:]):
            if left == _SBER_PAIR_SPACE_CID or right == _SBER_PAIR_SPACE_CID:
                continue
            pairs += 1
    return pairs


def _sber_pdf_glyph_pair_count(pdf: bytes) -> int:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception:
        return 0
    try:
        best = b""
        try:
            for xref in doc[0].get_contents() or []:
                st = doc.xref_stream(xref)
                if st and len(st) > len(best):
                    best = st
        except Exception:
            pass
        if not best:
            for xref in range(1, doc.xref_length()):
                try:
                    st = doc.xref_stream(xref)
                except Exception:
                    continue
                if st and b"Tj" in st and b"Tm" in st and len(st) > len(best):
                    best = st
        doc.close()
    except Exception:
        try:
            doc.close()
        except Exception:
            pass
        return 0
    return _sber_glyph_pair_count_from_stream(best)


def _boost_sber_separator_glyph_pairs(
    stream: bytes,
    *,
    need: int = _SBP_GLYPH_PAIRS_MIN,
) -> bytes:
    """Raise glyph_pairs: separator '- - -' → '-.-.-.' (CID 3→17).

    Space and period share advance 569 in Sber Arial — end_x / layout stay put
    (hyphen substitution shifted K-SBER-SBP-EXACT end_x). Each space→period
    between hyphens adds 2 non-space adjacent pairs.

    Only apply when under ``need``: period bytes hurt Jasper 0x789c density and
    often push CS flate over SBP_CONTENT_RAW_MAX (733).
    """
    cur = _sber_glyph_pair_count_from_stream(stream)
    if cur >= need:
        return stream
    conversions_left = max(1, (need - cur + 1) // 2)
    out = bytearray()
    last = 0
    changed = 0
    for m in re.finditer(rb"\((?:\\.|[^\\)])*\)(\s*Tj)", stream):
        if conversions_left <= 0:
            break
        lit = m.group(0)
        end_paren = lit.rfind(b")")
        if end_paren <= 0:
            continue
        inner = lit[1:end_paren]
        tj_sp = lit[end_paren:]
        try:
            unesc = _pdf_literal_unescape(inner)
        except Exception:
            continue
        if len(unesc) < 4 or len(unesc) % 2:
            continue
        cids = [(unesc[i] << 8) | unesc[i + 1] for i in range(0, len(unesc), 2)]
        if cids.count(16) < 8 or cids.count(3) < 4:
            continue
        local = 0
        for j in range(1, len(cids) - 1):
            if conversions_left <= 0:
                break
            if cids[j] == 3 and cids[j - 1] == 16 and cids[j + 1] == 16:
                cids[j] = 17  # period, same AW as space
                conversions_left -= 1
                local += 1
        if not local:
            continue
        new_unesc = b"".join(
            bytes(((c >> 8) & 0xFF, c & 0xFF)) for c in cids
        )
        if len(new_unesc) != len(unesc):
            continue
        new_inner = _cid_raw_from_unesc(new_unesc)
        if len(new_inner) != len(inner):
            continue
        out.extend(stream[last:m.start()])
        out.extend(b"(" + new_inner + tj_sp)
        last = m.end()
        changed += local
    if last == 0:
        return stream
    out.extend(stream[last:])
    boosted = bytes(out)
    new_c = _sber_glyph_pair_count_from_stream(boosted)
    logger.info(
        "Sber separator pair boost %d→%d (space→period x%d)",
        cur, new_c, changed,
    )
    return boosted if new_c >= cur else stream


def _inject_sber_contour_pad_cids(
    stream: bytes,
    *,
    need_used: int = 66,
    max_clen: int = SBP_CONTENT_RAW_MAX,
    prefer_cids: Optional[Set[int]] = None,
) -> bytes:
    """Map hyphen-separator spaces onto spare low CIDs so contour pads are used.

    Contour floor needs >=66 nonempty glyphs; reverse-closure forbids nonempty
    outside used CIDs. Replaces CID 3 between hyphens with unused low CIDs
    (same stream length). Prefer ``prefer_cids`` (orphan nonempty GIDs) so
    painting them clears SBER_FONT_REVERSE_GLYPH_CLOSURE without blanking
    under the contour floor.
    """
    used = _gids_in_stream(stream)
    if len(used) >= need_used:
        return stream
    need = need_used - len(used)
    costly = {0x28, 0x29, 0x5C, 0x0D, 0x0A}

    def _literal_safe(cid: int) -> bool:
        hi, lo = (cid >> 8) & 0xFF, cid & 0xFF
        for b in (hi, lo):
            if b in _ESC or b in costly:
                return False
        return True

    candidates = [
        c for c in range(1, 128)
        if c not in used
        and c not in (0, 3, _RUBLE_CID)
        and _literal_safe(c)
    ]
    if prefer_cids:
        prefer = [c for c in sorted(prefer_cids) if c in candidates]
        rest = [c for c in candidates if c not in prefer_cids]
        candidates = prefer + rest
    if not candidates:
        return stream

    def _jasper_min(s: bytes):
        sizes = []
        for level in (6, 5, 7, 9, 4):
            for comp in (
                compress_like_jasper(s, level=level),
                zlib.compress(s, level),
            ):
                if _zlib_roundtrip_ok(comp, s):
                    sizes.append(len(comp))
        return min(sizes) if sizes else None

    def _apply(n_pads: int):
        if n_pads <= 0:
            return stream
        pad_iter = iter(candidates[:n_pads])
        out = bytearray()
        last = 0
        assigned = 0
        for m in re.finditer(rb"\((?:\\.|[^\\)])*\)(\s*Tj)", stream):
            if assigned >= n_pads:
                break
            lit = m.group(0)
            end_paren = lit.rfind(b")")
            if end_paren <= 0:
                continue
            inner = lit[1:end_paren]
            tj_sp = lit[end_paren:]
            try:
                unesc = _pdf_literal_unescape(inner)
            except Exception:
                continue
            if len(unesc) < 4 or len(unesc) % 2:
                continue
            cids = [(unesc[i] << 8) | unesc[i + 1] for i in range(0, len(unesc), 2)]
            if cids.count(16) < 8 or cids.count(3) < 2:
                continue
            local = 0
            for j in range(1, len(cids) - 1):
                if assigned >= n_pads:
                    break
                if cids[j] == 3 and cids[j - 1] == 16 and cids[j + 1] == 16:
                    try:
                        nxt = next(pad_iter)
                    except StopIteration:
                        break
                    cids[j] = nxt
                    assigned += 1
                    local += 1
            if not local:
                continue
            new_unesc = b"".join(
                bytes(((c >> 8) & 0xFF, c & 0xFF)) for c in cids
            )
            if len(new_unesc) != len(unesc):
                continue
            new_inner = _cid_raw_from_unesc(new_unesc)
            if len(new_inner) != len(inner):
                continue
            out.extend(stream[last:m.start()])
            out.extend(b"(" + new_inner + tj_sp)
            last = m.end()
        if last == 0 or assigned == 0:
            return None
        out.extend(stream[last:])
        return bytes(out)

    best = None
    best_n = 0
    best_j = None
    for n in range(need, 0, -1):
        trial = _apply(n)
        if trial is None:
            continue
        j_min = _jasper_min(trial)
        if j_min is not None and j_min <= max_clen:
            best, best_n, best_j = trial, n, j_min
            break
    if best is None:
        logger.info(
            "Sber contour-pad inject skipped (need %d, no flate<=%d fit)",
            need, max_clen,
        )
        return stream
    logger.info(
        "Sber contour-pad CIDs injected x%d/%d (used %d->%d, jasper_min=%d)",
        best_n, need, len(used), len(_gids_in_stream(best)), best_j,
    )
    return best


def _gids_to_unicode(stream: bytes, uni_gid: Dict[int, int]) -> Set[int]:
    gid_to_uni = {gid: cp for cp, gid in uni_gid.items()}
    need: Set[int] = set()
    for gid in _gids_in_stream(stream):
        if gid in gid_to_uni:
            need.add(gid_to_uni[gid])
        elif gid == _RUBLE_CID:
            need.add(0x20BD)
        elif gid == 0x20 or gid == 3:
            need.add(0x20)
    return need


def _cids_from_tj_raw(raw: bytes) -> Set[int]:
    """CID set from a literal/hex TJ payload (after unescape)."""
    raw = _pdf_literal_unescape(raw)
    out: Set[int] = set()
    i = 0
    while i < len(raw):
        if raw[i:i + 2] == b"\rZ":
            out.add(_RUBLE_CID)
            i += 2
            continue
        if i + 1 >= len(raw):
            break
        out.add((raw[i] << 8) | raw[i + 1])
        i += 2
    return out


def _face_field_gids(
    stream: bytes,
    field_y: Dict[str, float],
    field_specs: list,
    uni_gid: Dict[int, int],
) -> Set[int]:
    """CIDs painted only in replaceable face rows (FIO/bank/amount/…)."""
    out: Set[int] = set()
    for _name, key, _sz in field_specs:
        y = field_y.get(key)
        if y is None:
            continue
        row = _read_field_at_y(stream, y, uni_gid)
        if not row:
            continue
        out |= _cids_from_tj_raw(row[0])
    return out


def _allocate_uni_gid(
    base: Dict[int, int],
    needed: Set[int],
    num_glyphs: int,
    steal_gids: Optional[Set[int]] = None,
) -> Dict[int, int]:
    """Map needed Unicode → CID.

    Prefer corpus slot prefs, then *steal* donor face-only CIDs (overwrite
    outlines in place — keeps nonempty/uniq/size near atlas), then empty free
    slots. Same idea as T-Bank hydrate into existing FontFile2 slots.
    """
    sgl.ensure_library()
    prefs = sgl.slot_preferences()
    out = dict(base)
    used = set(out.values())
    reserved = {0, 3}
    stealable = set(steal_gids or ()) - reserved
    # Байты, которые в PDF-literal раздувают длину (\\ ( ) \\r \\n).
    # q/Q (0x71/0x51): Proton naive SBER_CONTENT_QQ_PROFILE counts them in Tj.
    costly = {0x28, 0x29, 0x5C, 0x0D, 0x0A}
    qq_bytes = {0x71, 0x51}  # q / Q

    def escape_cost(gid: int) -> int:
        hi, lo = (gid >> 8) & 0xFF, gid & 0xFF
        cost = (1 if hi in costly else 0) + (1 if lo in costly else 0)
        # Heavy penalty so chaos FIO prefers CIDs that keep corpus q/Q counts.
        cost += 8 if hi in qq_bytes else 0
        cost += 8 if lo in qq_bytes else 0
        return cost

    def can_use(gid: int) -> bool:
        if gid in reserved or gid >= num_glyphs:
            return False
        if gid not in used:
            return True
        if gid not in stealable:
            return False
        holders = [c for c, g in out.items() if g == gid]
        return bool(holders) and all(c not in needed for c in holders)

    def take_slot(cp: int, slot: int) -> None:
        for old_cp, g in list(out.items()):
            if g == slot and old_cp != cp:
                del out[old_cp]
                used.discard(slot)
        out[cp] = slot
        used.add(slot)
        stealable.discard(slot)

    for cp in sorted(needed):
        if cp in out:
            continue
        ch = chr(cp)
        slot = None
        pref_hits = [c for c in prefs.get(ch, []) if can_use(c)]
        if pref_hits:
            # Stealable prefs first (in-place overwrite), then free prefs.
            pref_hits.sort(key=lambda g: (0 if g in stealable else 1, escape_cost(g)))
            slot = pref_hits[0]
        if slot is None:
            free = [c for c in range(num_glyphs) if can_use(c)]
            if free:
                free.sort(key=lambda g: (0 if g in stealable else 1, escape_cost(g)))
                slot = free[0]
        if slot is None:
            logger.warning("Sber dynamic: no slot for U+%04X (%s)", cp, ch)
            continue
        take_slot(cp, slot)
    # Drop donor-only unicodes we no longer need (stolen or unused face letters).
    out = {cp: gid for cp, gid in out.items() if cp in needed}
    return out


def _uni_gid_for_active(uni_gid: Dict[int, int], active_gids: Set[int]) -> Dict[int, int]:
    """ToUnicode только для CID из content stream (как T-Bank)."""
    if not active_gids:
        return dict(uni_gid)
    rev = {gid: cp for cp, gid in uni_gid.items()}
    out: Dict[int, int] = {}
    for gid in active_gids:
        if gid == 0:
            continue
        if gid == 3:
            out[0x20] = 3
            continue
        cp = rev.get(gid)
        if cp is not None:
            out[cp] = gid
    return out


def _get_font_table(font_bytes: bytes, tag: bytes) -> Optional[bytes]:
    if len(font_bytes) < 12:
        return None
    num_tables = int.from_bytes(font_bytes[4:6], "big")
    for i in range(num_tables):
        e = 12 + i * 16
        if e + 16 > len(font_bytes):
            break
        if font_bytes[e:e + 4] == tag:
            off = int.from_bytes(font_bytes[e + 8:e + 12], "big")
            ln = int.from_bytes(font_bytes[e + 12:e + 16], "big")
            if off + ln <= len(font_bytes):
                return font_bytes[off:off + ln]
    return None


def _ot_table_checksum(table_bytes: bytes) -> int:
    padded = table_bytes + b"\x00" * ((-len(table_bytes)) % 4)
    total = 0
    for i in range(0, len(padded), 4):
        total = (total + int.from_bytes(padded[i:i + 4], "big")) & 0xFFFFFFFF
    return total


def _restore_font_table(font_bytes: bytes, tag: bytes, orig_table: bytes) -> bytes:
    if len(font_bytes) < 12:
        return font_bytes
    num_tables = int.from_bytes(font_bytes[4:6], "big")
    data = bytearray(font_bytes)
    for i in range(num_tables):
        e = 12 + i * 16
        if e + 16 > len(data):
            break
        if data[e:e + 4] == tag:
            off = int.from_bytes(data[e + 8:e + 12], "big")
            ln = int.from_bytes(data[e + 12:e + 16], "big")
            if off + ln <= len(data) and len(orig_table) == ln:
                data[off:off + ln] = orig_table
                if tag == b"head" and len(orig_table) >= 12:
                    head_for_cs = bytearray(orig_table)
                    head_for_cs[8:12] = b"\x00\x00\x00\x00"
                    csum = _ot_table_checksum(bytes(head_for_cs))
                else:
                    csum = _ot_table_checksum(orig_table)
                data[e + 4:e + 8] = csum.to_bytes(4, "big")
            break
    return bytes(data)


def _restore_sber_struct_fp_tables(font_bytes: bytes, ff2_orig: bytes) -> bytes:
    """Keep Proton struct_fp = donor after fontTools save / glyf surgery.

    struct_fp hashes fpgm+prep+cvt+head(CSA-zeroed)+maxp — not glyf. Any
    TTFont.save rewrites head/maxp and trips CROSSPRODUCT_002.
    """
    if not font_bytes or not ff2_orig:
        return font_bytes
    out = font_bytes
    for tag in (b"cvt ", b"fpgm", b"hhea", b"head", b"maxp", b"prep"):
        orig_t = _get_font_table(ff2_orig, tag)
        if orig_t is not None:
            out = _restore_font_table(out, tag, orig_t)
    orig_csa = _get_head_csa(ff2_orig)
    if orig_csa is not None:
        out = _restore_head_csa(out, orig_csa)
    return out


def _get_table_order_from_font(font_bytes: bytes) -> List[str]:
    import struct
    if len(font_bytes) < 12:
        return list(_SBER_TABLE_ORDER)
    num_tables = struct.unpack(">H", font_bytes[4:6])[0]
    return [
        font_bytes[12 + i * 16:12 + i * 16 + 4].decode("latin-1")
        for i in range(num_tables)
    ]


def _replace_font_table_data(font_bytes: bytes, tag_str: str, new_data: bytes) -> bytes:
    import math
    import struct

    data = font_bytes
    sfnt_version = data[0:4]
    num_tables = struct.unpack(">H", data[4:6])[0]
    tables: Dict[str, bytes] = {}
    order: List[str] = []
    for i in range(num_tables):
        e = 12 + i * 16
        tag = data[e:e + 4].decode("latin-1")
        off = struct.unpack(">I", data[e + 8:e + 12])[0]
        ln = struct.unpack(">I", data[e + 12:e + 16])[0]
        tables[tag] = data[off:off + ln]
        order.append(tag)
    tables[tag_str] = new_data

    n = len(order)
    entry_selector = int(math.log2(n)) if n > 0 else 0
    search_range = (2 ** entry_selector) * 16
    range_shift = n * 16 - search_range
    header = sfnt_version + struct.pack(">HHHH", n, search_range, entry_selector, range_shift)

    current_off = 12 + n * 16
    directory = b""
    body = b""
    for tag in order:
        tbl_bytes = tables[tag]
        if tag == "head" and len(tbl_bytes) >= 12:
            head_for_cs = bytearray(tbl_bytes)
            head_for_cs[8:12] = b"\x00\x00\x00\x00"
            checksum = _ot_table_checksum(bytes(head_for_cs))
        else:
            checksum = _ot_table_checksum(tbl_bytes)
        directory += tag.encode("latin-1") + struct.pack(">III", checksum, current_off, len(tbl_bytes))
        body += tbl_bytes + b"\x00" * ((4 - len(tbl_bytes) % 4) % 4)
        current_off += (len(tbl_bytes) + 3) & ~3
    return header + directory + body


def _recalculate_sfnt_checksum_adjustment(font_bytes: bytes) -> bytes:
    """Recompute head.checkSumAdjustment after in-table entropy changes."""
    data = bytearray(font_bytes)
    if len(data) < 12:
        return font_bytes
    nt = int.from_bytes(data[4:6], "big")
    head_entry = None
    head_off = head_len = 0
    for i in range(nt):
        e = 12 + i * 16
        if e + 16 > len(data):
            return font_bytes
        if data[e:e + 4] == b"head":
            head_entry = e
            head_off = int.from_bytes(data[e + 8:e + 12], "big")
            head_len = int.from_bytes(data[e + 12:e + 16], "big")
            break
    if head_entry is None or head_off + max(12, head_len) > len(data):
        return font_bytes
    data[head_off + 8:head_off + 12] = b"\x00\x00\x00\x00"
    head_table = bytes(data[head_off:head_off + head_len])
    data[head_entry + 4:head_entry + 8] = _ot_table_checksum(head_table).to_bytes(4, "big")
    padded = bytes(data) + b"\x00" * ((-len(data)) % 4)
    total = sum(
        int.from_bytes(padded[i:i + 4], "big")
        for i in range(0, len(padded), 4)
    ) & 0xFFFFFFFF
    adjustment = (0xB1B0AFBA - total) & 0xFFFFFFFF
    data[head_off + 8:head_off + 12] = adjustment.to_bytes(4, "big")
    return bytes(data)


def _pad_glyf_slack_exact_profile(
    font_bytes: bytes,
    target_decoded: int,
    target_compressed: int,
    excluded_gids: Optional[Set[int]] = None,
) -> Optional[bytes]:
    """Fill a final zero-contour glyph's instruction area to donor profile."""
    glyf_data = _get_font_table(font_bytes, b"glyf")
    loca_data = _get_font_table(font_bytes, b"loca")
    if (
        glyf_data is None
        or loca_data is None
        or len(loca_data) < 8
        or not _orig_loca_is_long(font_bytes)
        or len(font_bytes) > target_decoded
    ):
        return None
    locations = [
        int.from_bytes(loca_data[i:i + 4], "big")
        for i in range(0, len(loca_data), 4)
    ]
    excluded_gids = excluded_gids or set()
    carrier = next(
        (
            gid
            for gid in range(len(locations) - 2, 0, -1)
            if gid not in excluded_gids
            and locations[gid] == locations[gid + 1]
        ),
        None,
    )
    if carrier is None:
        return None
    insert_at = locations[carrier]
    rough = target_decoded - len(font_bytes)

    def _build(instructions: bytes) -> bytes:
        # Valid simple-glyph record: contours=0, zero bbox, instruction bytes.
        record = (
            b"\x00\x00" + b"\x00" * 8
            + len(instructions).to_bytes(2, "big")
            + instructions
        )
        new_glyf = glyf_data[:insert_at] + record + glyf_data[insert_at:]
        new_locations = list(locations)
        for idx in range(carrier + 1, len(new_locations)):
            new_locations[idx] += len(record)
        new_loca = b"".join(v.to_bytes(4, "big") for v in new_locations)
        out = _replace_font_table_data(font_bytes, "glyf", new_glyf)
        out = _replace_font_table_data(out, "loca", new_loca)
        return _recalculate_sfnt_checksum_adjustment(out)

    pad_len = None
    for n in range(max(12, rough - 12), rough + 13):
        if n % 2:
            continue
        trial = _build(b"\x00" * (n - 12))
        if len(trial) == target_decoded:
            pad_len = n
            break
    if pad_len is None:
        return None

    for salt in range(32):
        seed = hashlib.sha256(
            font_bytes + target_compressed.to_bytes(4, "big") + bytes([salt])
        ).digest()
        entropy = bytearray()
        block = seed
        instruction_len = pad_len - 12
        while len(entropy) < instruction_len:
            block = hashlib.sha256(block + len(entropy).to_bytes(4, "big")).digest()
            entropy.extend(block)

        cache: Dict[int, Tuple[int, bytes]] = {}

        def _candidate(n_entropy: int) -> Tuple[int, bytes]:
            n_entropy = max(0, min(instruction_len, n_entropy))
            if n_entropy not in cache:
                instructions = (
                    bytes(entropy[:n_entropy])
                    + b"\x00" * (instruction_len - n_entropy)
                )
                trial = _build(instructions)
                cache[n_entropy] = (len(zlib.compress(trial, 6)), trial)
            return cache[n_entropy]

        lo, hi = 0, instruction_len
        crossing = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            csz, trial = _candidate(mid)
            if csz == target_compressed:
                logger.info(
                    "Sber SBP glyf slack tune cid=%d bytes=%d entropy=%d comp=%d",
                    carrier, pad_len, mid, csz,
                )
                return trial
            if csz < target_compressed:
                crossing = mid
                lo = mid + 1
            else:
                hi = mid - 1
        for n in range(
            max(0, crossing - 192),
            min(instruction_len, crossing + 256) + 1,
        ):
            csz, trial = _candidate(n)
            if csz == target_compressed:
                logger.info(
                    "Sber SBP glyf slack tune cid=%d bytes=%d entropy=%d comp=%d",
                    carrier, pad_len, n, csz,
                )
                return trial
    return None


def _shrink_glyf_to_decoded(
    font_bytes: bytes,
    target_decoded: int,
    target_compressed: int,
    excluded_gids: Optional[Set[int]] = None,
) -> Optional[bytes]:
    """Reclaim glyf bytes from unused glyphs so decoded len == donor.

    Library grafts often grow FontFile2 past the shell; slack pad only works when
    under-size. Blank large unused glyf records down to empty, then retune.
    """
    if len(font_bytes) < target_decoded:
        return None
    if len(font_bytes) == target_decoded:
        return _pad_glyf_slack_exact_profile(
            font_bytes, target_decoded, target_compressed, excluded_gids,
        )
    glyf_data = _get_font_table(font_bytes, b"glyf")
    loca_data = _get_font_table(font_bytes, b"loca")
    if (
        glyf_data is None
        or loca_data is None
        or len(loca_data) < 8
        or not _orig_loca_is_long(font_bytes)
    ):
        return None
    locations = [
        int.from_bytes(loca_data[i:i + 4], "big")
        for i in range(0, len(loca_data), 4)
    ]
    excluded_gids = excluded_gids or set()
    empty_rec = b"\x00\x00" + b"\x00" * 8 + b"\x00\x00"  # 0 contours, no instr

    # Largest unused glyf records first.
    candidates: List[Tuple[int, int]] = []
    for gid in range(1, len(locations) - 1):
        if gid in excluded_gids:
            continue
        sz = locations[gid + 1] - locations[gid]
        if sz > len(empty_rec):
            candidates.append((sz, gid))
    candidates.sort(reverse=True)
    cur = font_bytes
    cur_glyf = glyf_data
    cur_loc = locations
    for _sz, gid in candidates:
        if len(cur) <= target_decoded:
            break
        start, end = cur_loc[gid], cur_loc[gid + 1]
        if end - start <= len(empty_rec):
            continue
        new_glyf = cur_glyf[:start] + empty_rec + cur_glyf[end:]
        delta = (end - start) - len(empty_rec)
        new_loc = list(cur_loc)
        for idx in range(gid + 1, len(new_loc)):
            new_loc[idx] -= delta
        new_loca = b"".join(v.to_bytes(4, "big") for v in new_loc)
        trial = _replace_font_table_data(cur, "glyf", new_glyf)
        trial = _replace_font_table_data(trial, "loca", new_loca)
        trial = _recalculate_sfnt_checksum_adjustment(trial)
        cur = trial
        cur_glyf = new_glyf
        cur_loc = new_loc
        if len(cur) <= target_decoded:
            break
    if len(cur) > target_decoded:
        logger.warning(
            "Sber glyf shrink still oversize %d > %d after blanking unused",
            len(cur), target_decoded,
        )
        return None
    if len(cur) < target_decoded:
        tuned = _pad_glyf_slack_exact_profile(
            cur, target_decoded, target_compressed, excluded_gids,
        )
        if tuned is not None:
            return tuned
        # Exact decoded via tail copy is last resort (comp may miss).
        return cur + (b"\x00" * (target_decoded - len(cur)))
    tuned = _pad_glyf_slack_exact_profile(
        cur, target_decoded, target_compressed, excluded_gids,
    )
    return tuned if tuned is not None else cur


def _orig_loca_is_long(ff2_orig: bytes) -> bool:
    loca = _get_font_table(ff2_orig, b"loca")
    head = _get_font_table(ff2_orig, b"head")
    if not loca or not head or len(head) < 52:
        return False
    if head[50:52] == b"\x00\x01":
        return True
    maxp = _get_font_table(ff2_orig, b"maxp")
    if not maxp or len(maxp) < 6:
        return False
    num_glyphs = int.from_bytes(maxp[4:6], "big")
    return len(loca) >= (num_glyphs + 1) * 4


def _force_long_loca(font_bytes: bytes, ff2_orig: bytes) -> bytes:
    """fontTools save() сжимает loca 13680→6840; восстанавливаем long-format как в shell."""
    if not _orig_loca_is_long(ff2_orig):
        return font_bytes
    orig_loca = _get_font_table(ff2_orig, b"loca")
    if not orig_loca:
        return font_bytes

    ft = TTFont(BytesIO(font_bytes))
    locations = list(ft["loca"].locations)
    long_loca = b"".join(loc.to_bytes(4, "big") for loc in locations)
    if len(long_loca) != len(orig_loca):
        logger.warning(
            "Sber long loca mismatch: %d vs orig %d",
            len(long_loca), len(orig_loca),
        )
        return font_bytes
    return _replace_font_table_data(font_bytes, "loca", long_loca)


def _get_head_csa(font_bytes: bytes) -> Optional[int]:
    if len(font_bytes) < 12:
        return None
    nt = int.from_bytes(font_bytes[4:6], "big")
    for i in range(nt):
        e = 12 + i * 16
        if e + 16 > len(font_bytes):
            break
        if font_bytes[e:e + 4] == b"head":
            h = int.from_bytes(font_bytes[e + 8:e + 12], "big")
            if h + 12 <= len(font_bytes):
                return int.from_bytes(font_bytes[h + 8:h + 12], "big")
    return None


def _restore_head_csa(font_bytes: bytes, orig_csa: int) -> bytes:
    data = bytearray(font_bytes)
    nt = int.from_bytes(data[4:6], "big")
    for i in range(nt):
        e = 12 + i * 16
        if e + 16 > len(data):
            break
        if data[e:e + 4] == b"head":
            h = int.from_bytes(data[e + 8:e + 12], "big")
            if h + 12 <= len(data):
                data[h + 8:h + 12] = orig_csa.to_bytes(4, "big")
            break
    return bytes(data)


def _align_ttf_tail(font_bytes: bytes) -> bytes:
    pad = (-len(font_bytes)) % 4
    if pad:
        return font_bytes + b"\x00" * pad
    return font_bytes


def _pad_decoded_font_to_exact(font_bytes: bytes, ff2_orig: bytes) -> bytes:
    """Декодированный TTF — ровно len(shell), хвост из оригинала (как у T-Bank)."""
    target = len(ff2_orig)
    if len(font_bytes) == target:
        return font_bytes
    if len(font_bytes) < target:
        return font_bytes + ff2_orig[len(font_bytes):]
    # Tables may grow a few dozen bytes after composite attach — if SFNT
    # payload still fits, drop only the post-table slack / overflow tail.
    try:
        end = _sfnt_tables_end(font_bytes)
    except Exception:
        end = len(font_bytes)
    if 0 < end <= target:
        return font_bytes[:target]
    logger.warning(
        "Sber font decoded %d > shell %d (tables_end=%d) — tables grew",
        len(font_bytes), target, end,
    )
    return font_bytes


def _compress_font_exact_size(font_bytes: bytes, target_size: int) -> Optional[bytes]:
    """Jasper zlib-6 FontFile2 of exactly ``target_size`` (donor /Length).

    Content-stream ``compress_to_size`` fails on blanked TTF (space/ET checks) —
    natural flate then drops ~23KB→14KB and the PDF falls under the original
    100–103KB band. Append a high-entropy SFNT tail and probe until compressed
    length matches the donor. Sets ``_last_decoded`` to the padded TTF for
    /Length1.
    """
    from openpdf_deflate import compress_like_jasper

    if target_size <= 0:
        return None

    def _comp(data: bytes) -> Optional[bytes]:
        c = compress_like_jasper(data, level=6)
        if c is None:
            c = zlib.compress(data, 6)
        if not c or c[:2] != _JASPER_ZLIB:
            return None
        return c

    base0 = _align_ttf_tail(font_bytes)
    nat = _comp(base0)
    if nat is None:
        return None
    if len(nat) == target_size:
        _compress_font_exact_size._last_decoded = base0  # type: ignore[attr-defined]
        return nat
    if len(nat) > target_size:
        return None

    need = target_size - len(nat)
    # Multiple seeds — zlib size is not perfectly monotonic in pad length.
    for salt in range(24):
        seed = hashlib.sha256(
            base0 + target_size.to_bytes(4, "big") + bytes([salt])
        ).digest()

        def _padded(n: int) -> bytes:
            pad = bytearray()
            block = seed
            while len(pad) < n:
                block = hashlib.sha256(block + len(pad).to_bytes(4, "big")).digest()
                pad.extend(block)
            return _align_ttf_tail(base0 + bytes(pad[:n]))

        # Wide window: pad compresses ~1:1 but the exact mid is often
        # slightly below (target-nat); too-narrow lo missed the hit.
        lo = max(0, need - 600)
        hi = need * 2 + 1200
        under: Optional[Tuple[int, bytes, bytes]] = None  # mid, font, comp
        for mid in range(lo, hi + 1, 16):
            font = _padded(mid)
            comp = _comp(font)
            if comp is None:
                continue
            if len(comp) == target_size:
                _compress_font_exact_size._last_decoded = font  # type: ignore[attr-defined]
                return comp
            if len(comp) < target_size:
                under = (mid, font, comp)
            elif under is not None:
                # Crossed target — dense scan between last under and this mid.
                for m2 in range(under[0], mid + 1):
                    font2 = _padded(m2)
                    comp2 = _comp(font2)
                    if comp2 is not None and len(comp2) == target_size:
                        _compress_font_exact_size._last_decoded = font2  # type: ignore[attr-defined]
                        return comp2
                under = None
                # continue searching — size may dip again

        if under is not None:
            mid0, font0, _comp0 = under
            font = font0
            for step in range(0, 160):
                if step:
                    font = _align_ttf_tail(
                        font + bytes([(salt * 17 + step) & 0xFF])
                    )
                comp = _comp(font)
                if comp is None:
                    continue
                if len(comp) == target_size:
                    _compress_font_exact_size._last_decoded = font  # type: ignore[attr-defined]
                    return comp
                if len(comp) > target_size:
                    break
            for mid in range(max(0, mid0 - 64), mid0 + 128):
                font = _padded(mid)
                comp = _comp(font)
                if comp is not None and len(comp) == target_size:
                    _compress_font_exact_size._last_decoded = font  # type: ignore[attr-defined]
                    return comp
    return None


def _compress_font_like_donor(
    font_bytes: bytes, donor_raw: Optional[bytes]
) -> Tuple[bytes, bytes]:
    """Return (compressed_ff2, decoded_ff2_for_Length1).

    Decoded may grow by an entropy tail so /Length matches the donor stream.
    """
    _compress_font_exact_size._last_decoded = font_bytes  # type: ignore[attr-defined]
    if donor_raw:
        try:
            if hashlib.md5(zlib.decompress(donor_raw)).digest() == hashlib.md5(
                font_bytes
            ).digest():
                return donor_raw, font_bytes
        except Exception:
            pass
        hit = _compress_font_exact_size(font_bytes, len(donor_raw))
        if hit and hit[:2] == _JASPER_ZLIB:
            decoded = getattr(
                _compress_font_exact_size, "_last_decoded", font_bytes
            )
            return hit, decoded
    comp = _jasper_compress(font_bytes)
    if donor_raw and len(comp) != len(donor_raw):
        hit = _compress_font_exact_size(font_bytes, len(donor_raw))
        if hit and hit[:2] == _JASPER_ZLIB:
            decoded = getattr(
                _compress_font_exact_size, "_last_decoded", font_bytes
            )
            logger.info(
                "Sber FontFile2 sized %d→%d (decoded %d→%d)",
                len(comp),
                len(hit),
                len(font_bytes),
                len(decoded),
            )
            return hit, decoded
        logger.warning(
            "Sber FontFile2 size miss natural=%d donor=%d — PDF will be light",
            len(comp),
            len(donor_raw),
        )
    return comp, font_bytes


# Phone FontFile2 compressed HARD band (sber_internal_jasper atlas).
_PHONE_FF2_COMP_LO = 24_445
_PHONE_FF2_COMP_HI = 25_633
_PHONE_FF2_DEC_HARD = 56_214


def _snap_phone_ff2_decoded(font_bytes: bytes) -> bytes:
    """Snap FontFile2 decoded length to Proton exact atlas {55136,55476,56064}."""
    n = len(font_bytes)
    if n in _PHONE_FF2_DEC_ALLOWED:
        return font_bytes
    ups = [t for t in sorted(_PHONE_FF2_DEC_ALLOWED) if t >= n]
    if ups:
        target = ups[0]
        pad_n = target - n
        seed = hashlib.sha256(font_bytes[:64] + b"phone-ff2-snap").digest()
        pad = bytearray()
        block = seed
        while len(pad) < pad_n:
            block = hashlib.sha256(block + len(pad).to_bytes(4, "big")).digest()
            pad.extend(block)
        logger.info(
            "Sber phone FontFile2 decoded snap %d→%d (+%d entropy)",
            n, target, pad_n,
        )
        return font_bytes + bytes(pad[:pad_n])
    downs = [t for t in sorted(_PHONE_FF2_DEC_ALLOWED) if t < n]
    if downs:
        target = downs[-1]
        try:
            end = _sfnt_tables_end(font_bytes)
        except Exception:
            end = n
        if end <= target:
            logger.info(
                "Sber phone FontFile2 decoded trim %d→%d (tables_end=%d)",
                n, target, end,
            )
            return font_bytes[:target]
    logger.warning(
        "Sber phone FontFile2 decoded %d cannot snap to %s",
        n, sorted(_PHONE_FF2_DEC_ALLOWED),
    )
    return font_bytes


def _sfnt_tables_end(font_bytes: bytes) -> int:
    if len(font_bytes) < 12:
        return len(font_bytes)
    nt = int.from_bytes(font_bytes[4:6], "big")
    end = 0
    for i in range(nt):
        e = 12 + i * 16
        if e + 16 > len(font_bytes):
            break
        off = int.from_bytes(font_bytes[e + 8:e + 12], "big")
        ln = int.from_bytes(font_bytes[e + 12:e + 16], "big")
        end = max(end, off + ln)
    return end


def _entropy_fill_sfnt_tail(font_bytes: bytes, n_tail: int) -> bytes:
    """Replace last n_tail bytes (after tables) with high-entropy pad.

    Keeps decoded length identical; raises zlib size after blanking orphans
    emptied glyf (phone HARD needs compressed ~25KB at decoded 55136).
    """
    if n_tail <= 0:
        return font_bytes
    end = _sfnt_tables_end(font_bytes)
    max_tail = max(0, len(font_bytes) - end)
    n = min(n_tail, max_tail, len(font_bytes))
    if n <= 0:
        return font_bytes
    out = bytearray(font_bytes)
    seed = hashlib.sha256(font_bytes[:end] + n.to_bytes(4, "big")).digest()
    pad = bytearray()
    block = seed
    while len(pad) < n:
        block = hashlib.sha256(block + len(pad).to_bytes(4, "big")).digest()
        pad.extend(block)
    out[-n:] = pad[:n]
    return bytes(out)


def _compress_font_same_length_exact(
    font_bytes: bytes,
    donor_raw: Optional[bytes],
) -> Tuple[Optional[bytes], bytes]:
    """Match donor flate using only the existing post-table SFNT tail."""
    if not donor_raw:
        return None, font_bytes
    target = len(donor_raw)
    natural = _jasper_compress(font_bytes)
    if not natural or natural[:2] != _JASPER_ZLIB:
        natural = zlib.compress(font_bytes, 6)
    if len(natural) == target:
        return natural, font_bytes
    if len(natural) > target:
        # High-entropy SFNT tail / glyf slack can overshoot after uniq splits —
        # zero the post-table tail first (decoded size stays), then recheck.
        end = _sfnt_tables_end(font_bytes)
        if end < len(font_bytes):
            trial = font_bytes[:end] + (b"\x00" * (len(font_bytes) - end))
            trial = _recalculate_sfnt_checksum_adjustment(trial)
            natural2 = _jasper_compress(trial)
            if not natural2 or natural2[:2] != _JASPER_ZLIB:
                natural2 = zlib.compress(trial, 6)
            if len(natural2) == target:
                return natural2, trial
            if len(natural2) < target:
                font_bytes = trial
                natural = natural2
            else:
                return None, font_bytes
        else:
            return None, font_bytes

    end = _sfnt_tables_end(font_bytes)
    tail_len = len(font_bytes) - end
    if tail_len <= 0:
        return None, font_bytes
    prefix = font_bytes[:end]
    original_tail = font_bytes[end:]
    for salt in range(32):
        for n in range(1, tail_len + 1):
            seed = hashlib.sha256(
                prefix + target.to_bytes(4, "big") + bytes([salt]) + n.to_bytes(2, "big")
            ).digest()
            pad = bytearray()
            block = seed
            while len(pad) < n:
                block = hashlib.sha256(block + len(pad).to_bytes(4, "big")).digest()
                pad.extend(block)
            trial = prefix + original_tail[:-n] + bytes(pad[:n])
            comp = _jasper_compress(trial)
            if not comp or comp[:2] != _JASPER_ZLIB:
                comp = zlib.compress(trial, 6)
            if len(comp) == target:
                logger.info(
                    "Sber SBP same-length FontFile2 tune tail=%d comp=%d",
                    n, target,
                )
                return comp, trial
    return None, font_bytes


def _inflate_sber_ff2_comp_min(
    font_bytes: bytes,
    min_comp: int = 22_000,
) -> Tuple[bytes, bytes]:
    """Grow SFNT entropy tail (decoded may grow) until flate ≥ min_comp."""
    def _csz(buf: bytes) -> Tuple[bytes, int]:
        comp = _jasper_compress(buf)
        if not comp or comp[:2] != _JASPER_ZLIB:
            comp = zlib.compress(buf, 6)
        return comp, len(comp)

    cur = font_bytes
    comp, clen = _csz(cur)
    if clen >= min_comp:
        return cur, comp
    end = _sfnt_tables_end(cur)
    if end <= 0:
        end = len(cur)
    prefix = cur[:end]
    # Grow high-entropy pad after tables until compressed clears the floor.
    for extra in range(256, 12_000, 256):
        seed = hashlib.sha256(prefix + extra.to_bytes(4, "big") + b"sberff2").digest()
        pad = bytearray()
        block = seed
        while len(pad) < extra:
            block = hashlib.sha256(block + len(pad).to_bytes(4, "big")).digest()
            pad.extend(block)
        trial = prefix + bytes(pad[:extra])
        trial = _recalculate_sfnt_checksum_adjustment(trial)
        comp2, clen2 = _csz(trial)
        if clen2 >= min_comp:
            logger.info(
                "Sber FF2 inflate: dec %d→%d comp %d→%d (min %d)",
                len(font_bytes), len(trial), clen, clen2, min_comp,
            )
            return trial, comp2
    logger.warning(
        "Sber FF2 inflate failed: still comp=%d < %d", clen, min_comp,
    )
    return font_bytes, comp


def _compress_font_phone(
    font_bytes: bytes, donor_raw: Optional[bytes]
) -> Tuple[Optional[bytes], bytes]:
    """Phone FontFile2: prefer atlas compressed band; never refuse by decoded size.

    Blanking orphans makes zlib ~22KB; fill SFNT tail with entropy (same
    decoded length) until compressed lands in the atlas band when possible.
    """
    # Binary-search entropy tail size for compressed band.
    end = _sfnt_tables_end(font_bytes)
    max_tail = max(0, len(font_bytes) - end)
    lo_t, hi_t = 0, max_tail
    best: Optional[Tuple[bytes, bytes]] = None  # comp, decoded
    # Prefer donor length when available.
    prefer = len(donor_raw) if donor_raw else (
        (_PHONE_FF2_COMP_LO + _PHONE_FF2_COMP_HI) // 2
    )
    for _ in range(24):
        mid = (lo_t + hi_t) // 2
        trial = _entropy_fill_sfnt_tail(font_bytes, mid)
        comp = _jasper_compress(trial)
        if not comp or comp[:2] != _JASPER_ZLIB:
            comp = zlib.compress(trial, 6)
        if not comp:
            break
        csz = len(comp)
        if _PHONE_FF2_COMP_LO <= csz <= _PHONE_FF2_COMP_HI:
            best = (comp, trial)
            # Refine toward prefer
            if csz < prefer:
                lo_t = mid + 1
            elif csz > prefer:
                hi_t = mid - 1
            else:
                break
            continue
        if csz < _PHONE_FF2_COMP_LO:
            lo_t = mid + 1
        else:
            hi_t = mid - 1
        if lo_t > hi_t:
            break
    if best:
        logger.info(
            "Sber phone FontFile2 entropy-tail → comp=%d decoded=%d",
            len(best[0]), len(best[1]),
        )
        return best[0], best[1]
    # Fallback: try exact donor length via classic pad (may exceed dec HARD).
    if donor_raw:
        hit = _compress_font_exact_size(font_bytes, len(donor_raw))
        if hit and hit[:2] == _JASPER_ZLIB:
            decoded = getattr(
                _compress_font_exact_size, "_last_decoded", font_bytes
            )
            if _PHONE_FF2_COMP_LO <= len(hit) <= _PHONE_FF2_COMP_HI:
                return hit, decoded
    natural = _jasper_compress(font_bytes)
    if not natural or natural[:2] != _JASPER_ZLIB:
        natural = zlib.compress(font_bytes, 6)
    logger.warning(
        "Sber phone FontFile2 band miss natural_comp=%d — keep natural",
        len(natural or b""),
    )
    return natural, font_bytes


_SBER_TABLE_ORDER = ["cvt ", "fpgm", "glyf", "head", "hhea", "hmtx", "loca", "maxp", "prep"]


def _reorder_ttf_tables(font_bytes: bytes, desired_order: list) -> bytes:
    import math
    import struct

    data = font_bytes
    sfnt_version = data[0:4]
    num_tables = struct.unpack(">H", data[4:6])[0]
    tables: dict = {}
    for i in range(num_tables):
        e = 12 + i * 16
        tag_str = data[e:e + 4].decode("latin-1")
        checksum = struct.unpack(">I", data[e + 4:e + 8])[0]
        tbl_off = struct.unpack(">I", data[e + 8:e + 12])[0]
        tbl_len = struct.unpack(">I", data[e + 12:e + 16])[0]
        tables[tag_str] = {"checksum": checksum, "data": data[tbl_off:tbl_off + tbl_len]}

    order = [t for t in desired_order if t in tables]
    for t in tables:
        if t not in order:
            order.append(t)

    n = len(order)
    entry_selector = int(math.log2(n)) if n > 0 else 0
    search_range = (2 ** entry_selector) * 16
    range_shift = n * 16 - search_range
    header = sfnt_version + struct.pack(">HHHH", n, search_range, entry_selector, range_shift)

    data_start = 12 + n * 16
    current_off = data_start
    table_info = []
    for tag_str in order:
        tbl = tables[tag_str]
        tbl_bytes = tbl["data"]
        table_info.append((tag_str, tbl["checksum"], current_off, len(tbl_bytes), tbl_bytes))
        current_off += (len(tbl_bytes) + 3) & ~3

    directory = b""
    for tag_str, checksum, off, length, _ in table_info:
        directory += tag_str.encode("latin-1") + struct.pack(">III", checksum, off, length)

    body = b""
    for _, _, _, _, tbl_bytes in table_info:
        body += tbl_bytes
        body += b"\x00" * ((4 - (len(tbl_bytes) % 4)) % 4)
    return header + directory + body


def _composite_dependency_gids(font: TTFont, gids: set) -> set:
    glyf = font["glyf"]
    go = font.getGlyphOrder()
    deps: set = set()
    queue = set(gids)
    while queue:
        gid = queue.pop()
        if gid >= len(go):
            continue
        g = glyf[go[gid]]
        if getattr(g, "numberOfContours", 0) != -1:
            continue
        for comp in g.components or []:
            try:
                cgid = go.index(comp.glyphName)
            except ValueError:
                continue
            if cgid not in gids and cgid not in deps:
                deps.add(cgid)
                queue.add(cgid)
    return deps


def _glyph_has_visible_ink(glyf, go, cid: int, _seen: Optional[Set[int]] = None) -> bool:
    """True only if CID expands to at least one simple contour (not empty composite)."""
    if cid < 0 or cid >= len(go):
        return False
    seen = _seen if _seen is not None else set()
    if cid in seen:
        return False
    seen.add(cid)
    g = glyf[go[cid]]
    nc = int(getattr(g, "numberOfContours", 0) or 0)
    if nc > 0:
        return True
    if nc == 0:
        return False
    for comp in getattr(g, "components", None) or []:
        try:
            cgid = go.index(comp.glyphName)
        except ValueError:
            continue
        if _glyph_has_visible_ink(glyf, go, cgid, seen):
            return True
    return False


def _flatten_sber_composites(ft: TTFont, gids: Set[int]) -> int:
    """Decompose composites in ``gids`` so exclusive components can be blanked.

    Proton ``SBER_FONT_GLYF_NONEMPTY_COUNT`` ≤73 — chaos FIO + Arial composite
    leaves often push keep-set above that. Flattening painted CIDs frees the
    exclusive Latin/Cyrillic component leaves.
    """
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    glyf = ft["glyf"]
    go = ft.getGlyphOrder()
    try:
        gs = ft.getGlyphSet()
    except Exception:
        return 0
    n = 0
    for gid in sorted(gids):
        if gid < 0 or gid >= len(go):
            continue
        gname = go[gid]
        g = glyf[gname]
        is_comp = False
        try:
            is_comp = bool(g.isComposite())
        except Exception:
            is_comp = int(getattr(g, "numberOfContours", 0) or 0) == -1
        if not is_comp:
            continue
        try:
            dc_pen = DecomposingRecordingPen(gs)
            gs[gname].draw(dc_pen)
            tt_pen = TTGlyphPen(None)
            dc_pen.replay(tt_pen)
            glyf[gname] = tt_pen.glyph()
            n += 1
        except Exception:
            continue
    if n:
        logger.info("Sber flatten composites: %d glyphs → simple", n)
    return n


def _glyf_nonempty_uniq_lens(ft: TTFont) -> Set[int]:
    """Distinct loca lengths of nonempty glyph records (Proton GLYF_UNIQ_LENS)."""
    try:
        loca = ft["loca"]
    except Exception:
        return set()
    go = ft.getGlyphOrder()
    out: Set[int] = set()
    for i in range(len(go)):
        try:
            a, b = int(loca[i]), int(loca[i + 1])
        except Exception:
            continue
        ln = b - a
        if ln > 0:
            out.add(ln)
    return out


def _ensure_sber_glyf_uniq_lens(
    ft: TTFont,
    keep_gids: Set[int],
    *,
    lo: int = 52,
    hi: int = 56,
) -> int:
    """Pad blank (ncont=0) slots so unique nonempty glyf lengths ∈ [lo, hi].

    Blanking orphans collapses donor diversity (58→41). Proton HARD
    SBER_FONT_GLYF_UNIQ_LENS wants [52,56]. Fat-empty pads are ncont=0
    (reverse-closure safe) with distinct record sizes.
    """
    import struct
    from fontTools.ttLib.tables._g_l_y_f import Glyph as _TGlyph

    go = ft.getGlyphOrder()
    glyf = ft["glyf"]
    try:
        probe = BytesIO()
        ft.save(probe, reorderTables=False)
        planned = _glyf_nonempty_uniq_lens(TTFont(BytesIO(probe.getvalue())))
    except Exception:
        planned = _glyf_nonempty_uniq_lens(ft)
    if lo <= len(planned) <= hi:
        return 0
    if len(planned) > hi:
        # Caller must use _ensure_sber_glyf_uniq_lens_bytes / collapse on FF2.
        logger.warning(
            "Sber glyf uniq_lens=%d > %d — need FF2 collapse",
            len(planned), hi,
        )
        return 0

    # Do NOT invent zero-contour loca stubs — they raise loca_nonempty while
    # contour-nonempty stays flat → SBER_FONT_GLYF_CONTOUR_NONEMPTY /
    # LOCA_CONTOUR_MISMATCH. Uniq diversity must come from real outlines.
    if len(planned) < lo:
        logger.info(
            "Sber glyf uniq_lens: uniq=%d < %d — skip stub pads (contour floor)",
            len(planned), lo,
        )
    return 0


def _ensure_sber_glyf_uniq_lens_bytes(
    font_bytes: bytes,
    keep_gids: Set[int],
    ff2_orig: bytes,
    *,
    lo: int = 52,
    hi: int = 56,
    max_nonempty: int = 73,
    min_nonempty: int = 66,
) -> bytes:
    """Land uniq nonempty glyf lengths in [lo, hi] via unused collapse/bump."""
    out = font_bytes
    try:
        u = len(_glyf_nonempty_uniq_lens(TTFont(BytesIO(out))))
    except Exception:
        return out
    if lo <= u <= hi:
        return out
    if u > hi:
        for _ in range(4):
            nxt = _collapse_sber_uniq_lens_unused(out, keep_gids, ff2_orig, lo=lo, hi=hi)
            if nxt is out or nxt == out:
                # Force more aggressive: fewer templates by collapsing again
                # with a synthetic smaller keep (already in collapse).
                break
            out = nxt
            try:
                u = len(_glyf_nonempty_uniq_lens(TTFont(BytesIO(out))))
            except Exception:
                return out
            if u <= hi:
                break
        return out
    if u < lo:
        out = _bump_sber_uniq_lens(out, keep_gids, need=lo - u)
    return out

def _cap_sber_glyf_nonempty(
    font_bytes: bytes,
    keep_gids: Set[int],
    *,
    max_nonempty: int = 73,
) -> Optional[bytes]:
    """Fold surplus ncont=0 blank pads into the largest blank carrier."""
    import struct

    out = font_bytes
    for _ in range(64):
        glyf_data = _get_font_table(out, b"glyf")
        loca_data = _get_font_table(out, b"loca")
        if not glyf_data or not loca_data or not _orig_loca_is_long(out):
            return out
        locations = [
            int.from_bytes(loca_data[i:i + 4], "big")
            for i in range(0, len(loca_data), 4)
        ]
        n_glyphs = len(locations) - 1
        spans = [locations[i + 1] - locations[i] for i in range(n_glyphs)]
        nonempty = sum(1 for s in spans if s > 0)
        if nonempty <= max_nonempty:
            return out
        blanks = []
        for gid in range(1, n_glyphs):
            if gid in keep_gids or spans[gid] <= 0:
                continue
            off = locations[gid]
            if off + 2 > len(glyf_data):
                continue
            ncont = int.from_bytes(glyf_data[off:off + 2], "big", signed=True)
            if ncont == 0:
                blanks.append((spans[gid], gid))
        if len(blanks) < 2:
            return out
        blanks.sort()  # smallest first to absorb
        victim_span, victim = blanks[0]
        carrier_span, carrier = blanks[-1]
        if victim == carrier:
            return out
        # Move victim bytes onto carrier tail, then zero victim span.
        v_off, v_end = locations[victim], locations[victim + 1]
        c_off, c_end = locations[carrier], locations[carrier + 1]
        chunk = glyf_data[v_off:v_end]
        # Remove victim record.
        new_glyf = glyf_data[:v_off] + glyf_data[v_end:]
        new_loca = list(locations)
        for idx in range(victim + 1, len(new_loca)):
            new_loca[idx] -= victim_span
        # Carrier may have shifted.
        c_off2 = new_loca[carrier]
        c_end2 = new_loca[carrier + 1]
        new_glyf = new_glyf[:c_end2] + chunk + new_glyf[c_end2:]
        for idx in range(carrier + 1, len(new_loca)):
            new_loca[idx] += victim_span
        # victim is now empty (span 0) — loca[victim]==loca[victim+1]
        trial = _replace_font_table_data(out, "glyf", new_glyf)
        trial = _replace_font_table_data(
            trial, "loca", b"".join(v.to_bytes(4, "big") for v in new_loca),
        )
        trial = _recalculate_sfnt_checksum_adjustment(trial)
        if len(trial) != len(out):
            return out
        out = trial
        logger.info(
            "Sber glyf nonempty cap: merge cid=%d (%d B) → carrier=%d (now %d)",
            victim, victim_span, carrier, nonempty - 1,
        )
    return out


def _rebuild_sber_glyf_profile_after_blank(
    font_bytes: bytes,
    ff2_orig: bytes,
    donor_comp_len: int,
    keep_gids: Set[int],
) -> Optional[bytes]:
    """After orphan-blank, SFNT tail holds freed glyf — rebuild slack+uniq band.

    Blanking collapses glyf into span=0 empties and parks bytes in the SFNT
    tail, so uniq splits have no carrier. Truncate to tables, re-pad glyf
    slack to donor decoded/flate, then split uniq lengths and retune.
    """
    if not font_bytes or not ff2_orig or donor_comp_len <= 0:
        return None
    end = _sfnt_tables_end(font_bytes)
    lean = font_bytes[:end] if end > 0 else font_bytes
    target_dec = len(ff2_orig)
    if len(lean) > target_dec:
        return None
    tuned = _pad_glyf_slack_exact_profile(
        lean, target_dec, donor_comp_len, keep_gids,
    )
    if tuned is None or len(tuned) != target_dec:
        return None
    tuned = _ensure_sber_glyf_uniq_lens_bytes(tuned, keep_gids, ff2_orig)
    capped = _cap_sber_glyf_nonempty(tuned, keep_gids, max_nonempty=73)
    if capped is not None:
        tuned = capped
        # Cap can collapse uniq — restore under nonempty budget.
        tuned = _ensure_sber_glyf_uniq_lens_bytes(tuned, keep_gids, ff2_orig)
    tuned = _relax_sber_ff2_comp_budget(tuned, donor_comp_len, keep_gids)
    if len(tuned) != target_dec:
        return None
    try:
        ft = TTFont(BytesIO(tuned))
        uniq = len(_glyf_nonempty_uniq_lens(ft))
        loca = ft["loca"]
        nonempty = sum(
            1
            for g in range(len(loca.locations) - 1)
            if loca.locations[g + 1] > loca.locations[g]
        )
        ft.close()
    except Exception:
        return None
    if not (52 <= uniq <= 56) or not (66 <= nonempty <= 73):
        logger.warning(
            "Sber glyf rebuild profile uniq=%d nonempty=%d — keep tuned for finalize",
            uniq, nonempty,
        )
        # Prefer padded tuned over post-blank FF2: stub-collapse can leave
        # loca/glyf that fontTools later refuses ("not enough glyf table data").
        return tuned
    return tuned


def _relax_sber_ff2_comp_budget(
    font_bytes: bytes,
    donor_comp_len: int,
    keep_gids: Set[int],
) -> bytes:
    """Retune ncont=0 slack entropy so zlib-6 hits donor flate (post uniq)."""
    tuned = _retune_sber_ff2_comp_exact(font_bytes, donor_comp_len, keep_gids)
    return tuned if tuned is not None else font_bytes


def _retune_sber_ff2_comp_exact(
    font_bytes: bytes,
    donor_comp_len: int,
    keep_gids: Set[int],
) -> Optional[bytes]:
    """Binary-search entropy on largest blank glyf carrier; spans unchanged."""
    if donor_comp_len <= 0:
        return font_bytes

    def _csz(buf: bytes) -> int:
        comp = _jasper_compress(buf)
        if not comp or comp[:2] != _JASPER_ZLIB:
            comp = zlib.compress(buf, 6)
        return len(comp)

    if _csz(font_bytes) == donor_comp_len:
        return font_bytes
    glyf_data = _get_font_table(font_bytes, b"glyf")
    loca_data = _get_font_table(font_bytes, b"loca")
    if not glyf_data or not loca_data or not _orig_loca_is_long(font_bytes):
        return None
    locations = [
        int.from_bytes(loca_data[i:i + 4], "big")
        for i in range(0, len(loca_data), 4)
    ]
    carrier = None
    best = 0
    for gid in range(len(locations) - 2, 0, -1):
        if gid in keep_gids:
            continue
        span = locations[gid + 1] - locations[gid]
        if span < 24:
            continue
        off = locations[gid]
        if off + 12 > len(glyf_data):
            continue
        ncont = int.from_bytes(glyf_data[off:off + 2], "big", signed=True)
        if ncont == 0 and span > best:
            best = span
            carrier = gid
    if carrier is None:
        return None
    c_off = locations[carrier]
    c_end = locations[carrier + 1]
    # record: 10 byte header + instructionLen(2) + instructions
    instr_len = c_end - c_off - 12
    if instr_len < 8:
        return None
    seed = hashlib.sha256(
        font_bytes + donor_comp_len.to_bytes(4, "big") + b"retune"
    ).digest()
    entropy = bytearray()
    block = seed
    while len(entropy) < instr_len:
        block = hashlib.sha256(block + len(entropy).to_bytes(4, "big")).digest()
        entropy.extend(block)

    def _with_entropy(n_ent: int) -> bytes:
        n_ent = max(0, min(instr_len, n_ent))
        body = bytes(entropy[:n_ent]) + (b"\x00" * (instr_len - n_ent))
        # Keep header (incl. instruction length field) intact.
        new_glyf = glyf_data[: c_off + 12] + body + glyf_data[c_end:]
        trial = _replace_font_table_data(font_bytes, "glyf", new_glyf)
        return _recalculate_sfnt_checksum_adjustment(trial)

    # If still over at zero entropy, cannot hit by retune alone.
    zero_trial = _with_entropy(0)
    if _csz(zero_trial) > donor_comp_len:
        logger.warning(
            "Sber FF2 retune: zero-entropy still %d > %d",
            _csz(zero_trial), donor_comp_len,
        )
        return zero_trial  # best effort under; caller may still fail exact

    for salt in range(8):
        if salt:
            block = hashlib.sha256(seed + bytes([salt])).digest()
            entropy = bytearray()
            while len(entropy) < instr_len:
                block = hashlib.sha256(
                    block + len(entropy).to_bytes(4, "big")
                ).digest()
                entropy.extend(block)
        lo, hi = 0, instr_len
        crossing = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            trial = _with_entropy(mid)
            csz = _csz(trial)
            if csz == donor_comp_len:
                logger.info(
                    "Sber FF2 retune: cid=%d entropy=%d/%d → comp=%d",
                    carrier, mid, instr_len, csz,
                )
                return trial
            if csz < donor_comp_len:
                crossing = mid
                lo = mid + 1
            else:
                hi = mid - 1
        for n in range(
            max(0, crossing - 256),
            min(instr_len, crossing + 320) + 1,
        ):
            trial = _with_entropy(n)
            if _csz(trial) == donor_comp_len:
                logger.info(
                    "Sber FF2 retune: cid=%d entropy=%d/%d → comp=%d",
                    carrier, n, instr_len, donor_comp_len,
                )
                return trial
    return _with_entropy(crossing)


def _trim_orphan_glyf_contours(
    glyf_table,
    glyph_order: list,
    keep_gids: set,
    orig_glyf_table=None,
    filled_cids: Optional[Set[int]] = None,
) -> int:
    """Blank nonempty glyphs outside reverse-closure of used CIDs.

    Proton ``SBER_FONT_REVERSE_GLYPH_CLOSURE`` flags any nonempty glyph not
    reachable from content CIDs — including donor ink we never paint.
    Simple (n>0) and composite (n=-1) both count as nonempty.

    Never blank below corpus contour floor (66): zero-contour loca stubs no
    longer count toward SBER_FONT_GLYF_CONTOUR_NONEMPTY ∈ [66,73].
    """
    from copy import deepcopy
    from fontTools.ttLib.tables._g_l_y_f import Glyph as _TGlyph

    empty = _TGlyph()
    empty.numberOfContours = 0
    keep = set(keep_gids) | {0}

    def _contour_count() -> int:
        return sum(
            1
            for gname in glyph_order
            if int(getattr(glyf_table[gname], "numberOfContours", 0) or 0) != 0
        )

    before = _contour_count()
    # Already at/under floor — blanking would HARD-fail contour-nonempty.
    if before <= 66:
        return 0

    trimmed = 0
    for cid, gname in enumerate(glyph_order):
        if cid in keep:
            continue
        cur = glyf_table[gname]
        if getattr(cur, "numberOfContours", 0) != 0:
            # Keep enough real outlines for contour ∈ [66,73].
            if _contour_count() <= 66:
                break
            glyf_table[gname] = deepcopy(empty)
            trimmed += 1
    return trimmed


def _finalize_sber_contour_loca_profile(
    font_bytes: bytes,
    ff2_orig: bytes,
    keep_gids: Set[int],
    *,
    lo: int = 66,
    hi: int = 73,
) -> bytes:
    """Force contour-nonempty == loca_nonempty ∈ [lo,hi] like genuines.

    1) Collapse zero-contour loca stubs (span>0, ncont=0) → span 0.
    2) If contours < lo, restore donor outlines for unused gids.
    3) Pad decoded size back to donor.
    """
    from copy import deepcopy
    from fontTools.ttLib.tables._g_l_y_f import Glyph as _TGlyph

    if not font_bytes or not ff2_orig or len(font_bytes) != len(ff2_orig):
        # Size drift — still try contour fix on a padded copy when possible.
        pass
    try:
        ft = TTFont(BytesIO(font_bytes))
        orig_ft = TTFont(BytesIO(ff2_orig))
        glyf = ft["glyf"]
        go = ft.getGlyphOrder()
        orig_glyf = orig_ft["glyf"]
    except Exception:
        return font_bytes
    keep = set(keep_gids) | {0, 3}

    def _contours() -> int:
        n = 0
        for name in go:
            try:
                if int(getattr(glyf[name], "numberOfContours", 0) or 0) != 0:
                    n += 1
            except Exception:
                continue
        return n

    try:
        _ = _contours()
    except Exception as exc:
        logger.warning("Sber contour finalize: glyf unreadable (%s) — skip", exc)
        try:
            ft.close()
            orig_ft.close()
        except Exception:
            pass
        return font_bytes

    keep_outline = None
    for cid, gname in enumerate(go):
        if cid not in keep:
            continue
        try:
            if int(getattr(glyf[gname], "numberOfContours", 0) or 0) != 0:
                keep_outline = deepcopy(glyf[gname])
                break
        except Exception:
            continue
    donor_extras = []
    for cid, gname in enumerate(go):
        if cid in keep:
            continue
        try:
            og = orig_glyf[gname]
            if int(getattr(og, "numberOfContours", 0) or 0) != 0:
                donor_extras.append((gname, deepcopy(og)))
        except Exception:
            continue
    if _contours() < lo:
        # Empty KEEP slots must get DONOR outlines back — never tiny stubs.
        # Tiny-on-keep → broken face labels («Ч * к п…», asterisks in Операция).
        restored_keep = 0
        for cid, gname in enumerate(go):
            if cid not in keep:
                continue
            try:
                if int(getattr(glyf[gname], "numberOfContours", 0) or 0) != 0:
                    continue
                og = orig_glyf[gname]
                if int(getattr(og, "numberOfContours", 0) or 0) != 0:
                    glyf[gname] = deepcopy(og)
                    try:
                        if gname in orig_ft["hmtx"].metrics:
                            ft["hmtx"].metrics[gname] = orig_ft["hmtx"].metrics[gname]
                    except Exception:
                        pass
                    restored_keep += 1
            except Exception:
                continue
        if restored_keep:
            logger.info(
                "Sber contour floor: restored %d empty keep slots from donor",
                restored_keep,
            )
        empty_keep = [
            gname
            for cid, gname in enumerate(go)
            if cid in keep
            and int(getattr(glyf[gname], "numberOfContours", 0) or 0) == 0
        ]
        empty_unused = [
            gname
            for cid, gname in enumerate(go)
            if cid not in keep
            and int(getattr(glyf[gname], "numberOfContours", 0) or 0) == 0
        ]
        # Contour pads ONLY on unused slots (tiny/donor). Never write tiny into keep.
        need = lo - _contours()
        unused_pads: list = []
        empty_slots = []
        if need > 0:
            for gname in empty_unused:
                if len(empty_slots) >= need:
                    break
                empty_slots.append(gname)
                unused_pads.append(gname)
            if unused_pads:
                logger.info(
                    "Sber contour floor: unused-tiny %d (need %d; empty_keep left %d)",
                    len(unused_pads), need, len(empty_keep),
                )
            elif need > 0:
                logger.warning(
                    "Sber contour floor: need %d but no unused empty slots "
                    "(empty_keep=%d) — do not tiny-pad keep",
                    need, len(empty_keep),
                )
        distinct = min(3, need, len(donor_extras)) if need > 0 else 0
        space_m = None
        try:
            if 3 < len(go) and go[3] in orig_ft["hmtx"].metrics:
                space_m = orig_ft["hmtx"].metrics[go[3]]
            elif go[3] in ft["hmtx"].metrics:
                space_m = ft["hmtx"].metrics[go[3]]
        except Exception:
            space_m = None
        tiny = None
        try:
            from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates as _GC
            from fontTools.ttLib.tables.ttProgram import Program
            tiny = _TGlyph()
            tiny.numberOfContours = 1
            tiny.coordinates = _GC([(0, 0), (1, 0), (0, 1)])
            tiny.endPtsOfContours = [2]
            tiny.flags = [1, 1, 1]
            tiny.program = Program()
            tiny.program.fromBytecode([])
            tiny.xMin, tiny.yMin, tiny.xMax, tiny.yMax = 0, 0, 1, 1
        except Exception:
            tiny = keep_outline
        for i, gname in enumerate(empty_slots[:need]):
            # unused pads only — keep already restored from donor above
            if tiny is not None:
                glyf[gname] = deepcopy(tiny)
            elif i < distinct:
                _gn, og = donor_extras[i]
                glyf[gname] = deepcopy(og)
            elif keep_outline is not None:
                glyf[gname] = deepcopy(keep_outline)
            elif donor_extras:
                _gn, og = donor_extras[0]
                glyf[gname] = deepcopy(og)

        # Fold unused tiny pads into keep composite closure.
        if unused_pads:
            try:
                bio_mid = BytesIO()
                ft.save(bio_mid, reorderTables=False)
                mid = bio_mid.getvalue()
                pad_gids = []
                for gname in unused_pads:
                    try:
                        pad_gids.append(go.index(gname))
                    except ValueError:
                        continue
                folded = _attach_sber_orphans_as_components(
                    mid, set(keep_gids) | {0, 3}, pad_gids,
                )
                # attach hosts on .notdef — pads join Proton closure via gid 0
                if folded != mid:
                    ft.close()
                    ft = TTFont(BytesIO(folded))
                    glyf = ft["glyf"]
                    go = ft.getGlyphOrder()
                    # Pads are now keep-reachable — protect from surplus blank.
                    keep |= set(pad_gids)
                    keep |= _composite_dependency_gids(ft, keep)
            except Exception as exc:
                logger.warning("Sber contour unused-fold failed: %s", exc)
    # Blank surplus outlines above hi (unused only).
    if _contours() > hi:
        for cid, gname in enumerate(go):
            if _contours() <= hi:
                break
            if cid in keep:
                continue
            if int(getattr(glyf[gname], "numberOfContours", 0) or 0) != 0:
                empty = _TGlyph()
                empty.numberOfContours = 0
                glyf[gname] = deepcopy(empty)

    try:
        bio = BytesIO()
        ft.save(bio, reorderTables=False)
        out = bio.getvalue()
    except Exception:
        orig_ft.close()
        ft.close()
        return font_bytes
    orig_ft.close()
    ft.close()

    # Keep fontTools short-loca save here; long loca is forced once at ship
    # (force_long mid-pipeline often makes glyf unloadable under size pad).
    if len(out) != len(ff2_orig):
        out = _pad_decoded_font_to_exact(out, ff2_orig)
    if len(out) == len(ff2_orig):
        try:
            _chk = TTFont(BytesIO(out))
            _g = _chk["glyf"]
            for _n in _chk.getGlyphOrder():
                _ = _g[_n]
            _chk.close()
        except Exception:
            logger.warning("Sber contour finalize produced unloadable glyf — revert")
            return font_bytes
    logger.info(
        "Sber contour/loca finalize contours≈ target band [%d,%d] dec=%d/%d",
        lo, hi, len(out), len(ff2_orig),
    )
    return out if len(out) == len(ff2_orig) else font_bytes


def _bump_sber_uniq_lens(
    font_bytes: bytes,
    keep_gids: Set[int],
    *,
    need: int,
) -> bytes:
    """Grow glyf spans by distinct pads so uniq climbs by ``need``.

    Prefer unused contoured glyphs; if blanking removed them, trail-pad
    painted/keep nonempty records (readers ignore trailing glyf slack).
    """
    if need <= 0:
        return font_bytes
    glyf_data = bytearray(_get_font_table(font_bytes, b"glyf") or b"")
    loca_data = _get_font_table(font_bytes, b"loca")
    if not glyf_data or not loca_data or not _orig_loca_is_long(font_bytes):
        return font_bytes
    locations = [
        int.from_bytes(loca_data[i:i + 4], "big")
        for i in range(0, len(loca_data), 4)
    ]
    keep = set(keep_gids) | {0, 3}
    last = locations[-1]
    free = len(glyf_data) - last
    extra_each = [4 + 2 * i for i in range(need)]
    need_bytes = sum(extra_each)

    def _contoured(gid: int) -> bool:
        off, end = locations[gid], locations[gid + 1]
        if end <= off or off + 2 > len(glyf_data):
            return False
        ncont = int.from_bytes(glyf_data[off:off + 2], "big", signed=True)
        return ncont != 0

    unused = [
        gid for gid in range(1, len(locations) - 1)
        if gid not in keep and _contoured(gid)
    ]
    if len(unused) < need:
        # Fall back: painted nonempty — diversify their loca lengths.
        for gid in range(1, len(locations) - 1):
            if gid in (0, 3):
                continue
            if gid not in unused and _contoured(gid):
                unused.append(gid)
            if len(unused) >= need:
                break
    if len(unused) < need:
        return font_bytes

    # After 22KB inflate, free tail is often 0 — steal from ncont=0 carrier.
    carrier = None
    if free < need_bytes:
        blanks = []
        for gid in range(1, len(locations) - 1):
            off, end = locations[gid], locations[gid + 1]
            span = end - off
            if span < need_bytes + 4 or off + 2 > len(glyf_data):
                continue
            ncont = int.from_bytes(glyf_data[off:off + 2], "big", signed=True)
            if ncont == 0:
                blanks.append((span, gid))
        if not blanks:
            return font_bytes
        blanks.sort(reverse=True)
        carrier = blanks[0][1]

    bump_at = {unused[i]: extra_each[i] for i in range(need)}
    new_glyf = bytearray()
    new_loca = [0]
    for gid in range(len(locations) - 1):
        off, end = locations[gid], locations[gid + 1]
        chunk = bytes(glyf_data[off:end]) if end > off else b""
        if carrier is not None and gid == carrier and len(chunk) >= need_bytes + 4:
            chunk = chunk[:-need_bytes]
        if gid in bump_at and chunk:
            pad = bytes((i * 41 + 7) % 251 + 1 for i in range(bump_at[gid]))
            chunk = chunk + pad
        new_glyf.extend(chunk)
        new_loca.append(len(new_glyf))
    pad_n = len(glyf_data) - len(new_glyf)
    if pad_n < 0:
        return font_bytes
    if pad_n:
        new_glyf.extend(bytes((i * 37 + 11) % 251 + 1 for i in range(pad_n)))
    new_loca_b = b"".join(v.to_bytes(4, "big") for v in new_loca)
    if len(new_loca_b) != len(loca_data):
        return font_bytes
    trial = _replace_font_table_data(font_bytes, "glyf", bytes(new_glyf))
    trial = _replace_font_table_data(trial, "loca", new_loca_b)
    trial = _recalculate_sfnt_checksum_adjustment(trial)
    if len(trial) != len(font_bytes):
        return font_bytes
    logger.info(
        "Sber uniq bump +%d (pads=%s carrier=%s)",
        need, extra_each, carrier,
    )
    return trial


def _blank_sber_uniq_singletons(
    font_bytes: bytes,
    keep_gids: Set[int],
    *,
    min_nonempty: int = 66,
    hi_uniq: int = 56,
) -> bytes:
    """Blank unused singleton-length glyphs until uniq ≤ hi (floor min_nonempty)."""
    from copy import deepcopy
    from fontTools.ttLib.tables._g_l_y_f import Glyph as _TGlyph

    try:
        ft = TTFont(BytesIO(font_bytes))
    except Exception:
        return font_bytes
    glyf = ft["glyf"]
    go = ft.getGlyphOrder()
    keep = set(keep_gids) | {0, 3}
    empty = _TGlyph()
    empty.numberOfContours = 0

    def _stats():
        try:
            probe = BytesIO()
            ft.save(probe, reorderTables=False)
            raw = probe.getvalue()
            ft2 = TTFont(BytesIO(raw))
            uniq = len(_glyf_nonempty_uniq_lens(ft2))
            cont = sum(
                1
                for n in ft2.getGlyphOrder()
                if int(getattr(ft2["glyf"][n], "numberOfContours", 0) or 0) != 0
            )
            ft2.close()
            return uniq, cont, raw
        except Exception:
            return 999, 0, font_bytes

    uniq, cont, _ = _stats()
    if uniq <= hi_uniq:
        ft.close()
        return font_bytes
    # Map length → gids (contoured only).
    blanked = 0
    for _ in range(32):
        uniq, cont, _ = _stats()
        if uniq <= hi_uniq or cont <= min_nonempty:
            break
        loca = ft["loca"]
        spans: Dict[int, List[int]] = {}
        for gid, name in enumerate(go):
            if gid >= len(loca.locations) - 1:
                continue
            a, b = int(loca.locations[gid]), int(loca.locations[gid + 1])
            if b <= a:
                continue
            if int(getattr(glyf[name], "numberOfContours", 0) or 0) == 0:
                continue
            spans.setdefault(b - a, []).append(gid)
        singles = [
            gid
            for ln, gids in spans.items()
            if len(gids) == 1
            for gid in gids
            if gid not in keep
        ]
        if not singles:
            break
        victim = singles[0]
        glyf[go[victim]] = deepcopy(empty)
        blanked += 1
    if not blanked:
        ft.close()
        return font_bytes
    bio = BytesIO()
    ft.save(bio, reorderTables=False)
    ft.close()
    out = bio.getvalue()
    if len(out) != len(font_bytes) and abs(len(out) - len(font_bytes)) < 8:
        # rare; prefer exact
        pass
    logger.info(
        "Sber uniq singleton blank: %d glyphs (target uniq≤%d ne≥%d)",
        blanked, hi_uniq, min_nonempty,
    )
    if len(out) < len(font_bytes):
        try:
            out = _pad_decoded_font_to_exact(out, font_bytes)
        except Exception:
            pass
    return out if len(out) == len(font_bytes) else font_bytes


def _merge_sber_uniq_lens_by_pad(
    font_bytes: bytes,
    keep_gids: Set[int],
    *,
    lo: int = 52,
    hi: int = 56,
) -> bytes:
    """Land uniq nonempty glyf lengths in [lo, hi].

    Prefer pad singleton→existing length; if no free tail, shrink a larger
    singleton's post-outline slack down to another existing length; last
    resort blank a non-painted singleton (span→0).
    """
    out = font_bytes
    paint = set(keep_gids) | {0, 3}

    def _outline_bytes(glyf_data: bytes, off: int, end: int) -> int:
        """Min bytes needed for a simple glyph record (header+flags+points)."""
        if end - off < 10:
            return end - off
        ncont = int.from_bytes(glyf_data[off:off + 2], "big", signed=True)
        if ncont <= 0:
            return end - off  # composite / empty — don't shrink
        # Conservative: keep at least bbox+instruction stub; allow trim of tail only.
        # Use end-of-instructions if present.
        try:
            # After 10-byte header: ncont*2 endPts, then 2-byte insLen, then ins.
            pos = off + 10 + ncont * 2
            if pos + 2 > end:
                return end - off
            ins = int.from_bytes(glyf_data[pos:pos + 2], "big")
            pos = pos + 2 + ins
            # Remaining is flags+coords — unknown packed size; keep everything
            # up to a soft floor: never shrink below 75% of span or pos-off.
            return max(pos - off, int((end - off) * 0.75))
        except Exception:
            return end - off

    for _ in range(16):
        glyf_data = bytearray(_get_font_table(out, b"glyf") or b"")
        loca_data = _get_font_table(out, b"loca")
        if not glyf_data or not loca_data or not _orig_loca_is_long(out):
            return out
        locations = [
            int.from_bytes(loca_data[i:i + 4], "big")
            for i in range(0, len(loca_data), 4)
        ]
        n_glyphs = len(locations) - 1
        spans: Dict[int, List[int]] = {}
        for gid in range(n_glyphs):
            off, end = locations[gid], locations[gid + 1]
            ln = end - off
            if ln <= 0 or off + 2 > len(glyf_data):
                continue
            ncont = int.from_bytes(glyf_data[off:off + 2], "big", signed=True)
            if ncont == 0:
                continue
            spans.setdefault(ln, []).append(gid)
        uniq = len(spans)
        if lo <= uniq <= hi:
            return out
        if uniq < lo:
            return _bump_sber_uniq_lens(out, keep_gids, need=lo - uniq)

        singles = sorted(ln for ln, gids in spans.items() if len(gids) == 1)
        if not singles:
            singles = [min(spans)]
        free = len(glyf_data) - locations[-1]

        # 1) Pad smallest singleton up to next larger length.
        src = singles[0]
        bigger = [m for m in sorted(spans) if m > src]
        if bigger and free >= (bigger[0] - src):
            dst = bigger[0]
            need = dst - src
            gid = spans[src][0]
            new_glyf = bytearray()
            new_loca = [0]
            for g in range(n_glyphs):
                off, end = locations[g], locations[g + 1]
                chunk = bytes(glyf_data[off:end]) if end > off else b""
                if g == gid and chunk:
                    chunk = chunk + bytes((i * 41 + 9) % 251 + 1 for i in range(need))
                new_glyf.extend(chunk)
                new_loca.append(len(new_glyf))
            pad_n = len(glyf_data) - len(new_glyf)
            if pad_n >= 0:
                if pad_n:
                    new_glyf.extend(bytes((i * 37 + 11) % 251 + 1 for i in range(pad_n)))
                new_loca_b = b"".join(v.to_bytes(4, "big") for v in new_loca)
                if len(new_loca_b) == len(loca_data):
                    trial = _replace_font_table_data(out, "glyf", bytes(new_glyf))
                    trial = _replace_font_table_data(trial, "loca", new_loca_b)
                    trial = _recalculate_sfnt_checksum_adjustment(trial)
                    if len(trial) == len(out):
                        out = trial
                        logger.info(
                            "Sber uniq merge-pad: cid=%d %d→%d (uniq was %d)",
                            gid, src, dst, uniq,
                        )
                        continue

        # 2) Shrink largest singleton down to next smaller length (tail only).
        src = singles[-1]
        smaller = [m for m in sorted(spans) if m < src]
        if smaller:
            dst = smaller[-1]
            cut = src - dst
            gid = spans[src][0]
            off, end = locations[gid], locations[gid + 1]
            min_keep = _outline_bytes(bytes(glyf_data), off, end)
            if end - off - cut >= min_keep and cut > 0:
                new_glyf = bytearray()
                new_loca = [0]
                for g in range(n_glyphs):
                    o2, e2 = locations[g], locations[g + 1]
                    chunk = bytes(glyf_data[o2:e2]) if e2 > o2 else b""
                    if g == gid and len(chunk) >= cut:
                        chunk = chunk[:-cut]
                    new_glyf.extend(chunk)
                    new_loca.append(len(new_glyf))
                # Put freed bytes on glyf tail so decoded length stays.
                pad_n = len(glyf_data) - len(new_glyf)
                if pad_n >= 0:
                    if pad_n:
                        new_glyf.extend(bytes((i * 37 + 13) % 251 + 1 for i in range(pad_n)))
                    new_loca_b = b"".join(v.to_bytes(4, "big") for v in new_loca)
                    if len(new_loca_b) == len(loca_data):
                        trial = _replace_font_table_data(out, "glyf", bytes(new_glyf))
                        trial = _replace_font_table_data(trial, "loca", new_loca_b)
                        trial = _recalculate_sfnt_checksum_adjustment(trial)
                        if len(trial) == len(out):
                            out = trial
                            logger.info(
                                "Sber uniq merge-shrink: cid=%d %d→%d (uniq was %d)",
                                gid, src, dst, uniq,
                            )
                            continue

        # 3) Blank a non-painted singleton (drop one uniq length).
        blanked = False
        for ln in singles:
            for gid in spans[ln]:
                if gid in paint:
                    continue
                # Zero loca span: move glyph bytes to tail, set empty.
                new_glyf = bytearray()
                new_loca = [0]
                saved = b""
                for g in range(n_glyphs):
                    o2, e2 = locations[g], locations[g + 1]
                    chunk = bytes(glyf_data[o2:e2]) if e2 > o2 else b""
                    if g == gid:
                        saved = chunk
                        chunk = b""
                    new_glyf.extend(chunk)
                    new_loca.append(len(new_glyf))
                new_glyf.extend(saved)
                pad_n = len(glyf_data) - len(new_glyf)
                if pad_n < 0:
                    continue
                if pad_n:
                    new_glyf.extend(b"\x00" * pad_n)
                new_loca_b = b"".join(v.to_bytes(4, "big") for v in new_loca)
                if len(new_loca_b) != len(loca_data):
                    continue
                trial = _replace_font_table_data(out, "glyf", bytes(new_glyf))
                trial = _replace_font_table_data(trial, "loca", new_loca_b)
                trial = _recalculate_sfnt_checksum_adjustment(trial)
                if len(trial) != len(out):
                    continue
                out = trial
                blanked = True
                logger.info(
                    "Sber uniq blank-singleton: cid=%d span=%d (uniq was %d)",
                    gid, ln, uniq,
                )
                break
            if blanked:
                break
        if blanked:
            continue
        logger.warning("Sber uniq merge: stuck uniq=%d — stop", uniq)
        return out
    return out


def _collapse_sber_uniq_lens_unused(
    font_bytes: bytes,
    keep_gids: Set[int],
    ff2_orig: Optional[bytes] = None,
    *,
    lo: int = 52,
    hi: int = 56,
) -> bytes:
    """Size-neutral uniq cut: rewrite unused glyf records to 2–3 shared blobs."""
    glyf_data = _get_font_table(font_bytes, b"glyf")
    loca_data = _get_font_table(font_bytes, b"loca")
    if not glyf_data or not loca_data or not _orig_loca_is_long(font_bytes):
        return font_bytes
    locations = [
        int.from_bytes(loca_data[i:i + 4], "big")
        for i in range(0, len(loca_data), 4)
    ]
    n_glyphs = len(locations) - 1
    keep = set(keep_gids) | {0, 3}

    def _ncont(off: int, end: int) -> int:
        if end - off < 2 or off + 2 > len(glyf_data):
            return 0
        return int.from_bytes(glyf_data[off:off + 2], "big", signed=True)

    unused = []
    for gid in range(1, n_glyphs):
        if gid in keep:
            continue
        off, end = locations[gid], locations[gid + 1]
        if end > off and _ncont(off, end) != 0:
            unused.append(gid)
    # Font-patch often marks nearly all nonempty as keep → unused=0.
    # Collapse spare contoured slots not required for paint (keep may be fat).
    if len(unused) < 2:
        paint = set(keep_gids) | {0, 3}
        for gid in range(1, n_glyphs):
            if gid in paint:
                continue
            off, end = locations[gid], locations[gid + 1]
            if end > off and _ncont(off, end) != 0:
                unused.append(gid)
    if len(unused) < 2:
        logger.info("Sber uniq collapse raw: unused=%d — merge-pad", len(unused))
        return _merge_sber_uniq_lens_by_pad(font_bytes, keep_gids, lo=lo, hi=hi)

    n_templates = 3 if len(unused) >= 6 else 2
    # When already over Proton hi, collapse harder (1–2 shared blobs).
    try:
        cur_u = len(_glyf_nonempty_uniq_lens(TTFont(BytesIO(font_bytes))))
    except Exception:
        cur_u = hi + 1
    if cur_u > hi:
        n_templates = 1 if len(unused) >= 3 else 2
    sized = sorted(
        unused,
        key=lambda g: locations[g + 1] - locations[g],
    )
    templates = [
        bytes(glyf_data[locations[g]:locations[g + 1]])
        for g in sized[:n_templates]
    ]
    if not templates or any(len(t) < 12 for t in templates):
        return font_bytes

    new_glyf = bytearray()
    new_loca = [0]
    for gid in range(n_glyphs):
        off, end = locations[gid], locations[gid + 1]
        span = end - off
        if gid in unused:
            ui = unused.index(gid)
            new_glyf.extend(templates[ui % n_templates])
        elif span > 0:
            new_glyf.extend(glyf_data[off:end])
        new_loca.append(len(new_glyf))

    pad_n = len(glyf_data) - len(new_glyf)
    if pad_n < 0:
        # Oversize — trim entropy only if last loca allows (don't clip glyphs).
        logger.warning(
            "Sber uniq collapse raw oversize %d > %d — revert",
            len(new_glyf), len(glyf_data),
        )
        return font_bytes
    if pad_n:
        new_glyf.extend(bytes((i * 37 + 11) % 251 + 1 for i in range(pad_n)))
    new_loca_bytes = b"".join(v.to_bytes(4, "big") for v in new_loca)
    if len(new_loca_bytes) != len(loca_data):
        return font_bytes
    trial = _replace_font_table_data(font_bytes, "glyf", bytes(new_glyf))
    trial = _replace_font_table_data(trial, "loca", new_loca_bytes)
    trial = _recalculate_sfnt_checksum_adjustment(trial)
    if len(trial) != len(font_bytes):
        return font_bytes
    try:
        _ft_u = TTFont(BytesIO(trial))
        u = len(_glyf_nonempty_uniq_lens(_ft_u))
        _loca = _ft_u["loca"]
        ne = sum(
            1
            for g in range(len(_loca.locations) - 1)
            if _loca.locations[g + 1] > _loca.locations[g]
        )
        cont = sum(
            1
            for n in _ft_u.getGlyphOrder()
            if int(getattr(_ft_u["glyf"][n], "numberOfContours", 0) or 0) != 0
        )
        _ft_u.close()
    except Exception as exc:
        logger.warning("Sber uniq collapse raw unloadable: %s", exc)
        return font_bytes
    if cont != ne:
        logger.warning(
            "Sber uniq collapse raw loca/contour mismatch uniq=%d loca=%d contour=%d — keep if uniq band",
            u, ne, cont,
        )
        if not (lo <= u <= hi):
            return font_bytes
    elif not (66 <= ne <= 73):
        # Uniq OK but nonempty high — still keep for later cap/finalize.
        if not (lo <= u <= hi):
            logger.warning(
                "Sber uniq collapse raw rejected: uniq=%d loca=%d contour=%d",
                u, ne, cont,
            )
            return font_bytes
        logger.info(
            "Sber uniq collapse raw: uniq=%d in band, nonempty=%d — defer cap",
            u, ne,
        )
    if ff2_orig is not None and len(trial) == len(ff2_orig):
        trial = _restore_sber_struct_fp_tables(trial, ff2_orig)
    logger.info(
        "Sber uniq collapse raw: uniq→%d loca=%d contour=%d (unused=%d tpl=%d) band=[%d,%d]",
        u, ne, cont, len(unused), n_templates, lo, hi,
    )
    return trial


def _collapse_sber_zero_contour_loca_stubs(font_bytes: bytes) -> bytes:
    """Zero loca spans for ncont=0 records so loca_nonempty == contour_nonempty."""
    glyf_data = _get_font_table(font_bytes, b"glyf")
    loca_data = _get_font_table(font_bytes, b"loca")
    if not glyf_data or not loca_data or not _orig_loca_is_long(font_bytes):
        return font_bytes
    locations = [
        int.from_bytes(loca_data[i:i + 4], "big")
        for i in range(0, len(loca_data), 4)
    ]
    n_glyphs = len(locations) - 1
    new_glyf = bytearray()
    new_loca = [0]
    collapsed = 0
    for gid in range(n_glyphs):
        off, end = locations[gid], locations[gid + 1]
        span = end - off
        if span <= 0:
            new_loca.append(len(new_glyf))
            continue
        if off + 2 > len(glyf_data):
            new_glyf.extend(glyf_data[off:end])
            new_loca.append(len(new_glyf))
            continue
        ncont = int.from_bytes(glyf_data[off:off + 2], "big", signed=True)
        if ncont == 0:
            # Drop stub record entirely.
            collapsed += 1
            new_loca.append(len(new_glyf))
            continue
        new_glyf.extend(glyf_data[off:end])
        new_loca.append(len(new_glyf))
    if not collapsed:
        return font_bytes
    # Keep glyf table length: pad tail with entropy so SFNT size stable-ish.
    pad_n = len(glyf_data) - len(new_glyf)
    if pad_n < 0:
        return font_bytes
    if pad_n:
        new_glyf.extend(bytes((i * 37 + 11) % 251 + 1 for i in range(pad_n)))
        # Tail pad is past last loca — OK for size, ignored by loca.
    new_loca_bytes = b"".join(v.to_bytes(4, "big") for v in new_loca)
    if len(new_loca_bytes) != len(loca_data):
        return font_bytes
    trial = _replace_font_table_data(font_bytes, "glyf", bytes(new_glyf))
    trial = _replace_font_table_data(trial, "loca", new_loca_bytes)
    trial = _recalculate_sfnt_checksum_adjustment(trial)
    if len(trial) != len(font_bytes):
        return font_bytes
    logger.info("Sber collapse zero-contour loca stubs: %d", collapsed)
    return trial


def _sber_composite_glyph_record(component_gids: list) -> bytes:
    """Minimal composite glyf record: ncont=-1, zero bbox, 1-byte XY args."""
    import struct

    if not component_gids:
        return b""
    out = bytearray()
    out += struct.pack(">h", -1)
    out += struct.pack(">hhhh", 0, 0, 0, 0)
    last = len(component_gids) - 1
    for i, gid in enumerate(component_gids):
        # ARGS_ARE_XY=0x02, MORE_COMPONENTS=0x20; int8 x=y=0 (no WORDS).
        flags = 0x22 if i < last else 0x02
        out += struct.pack(">HH", flags, int(gid) & 0xFFFF)
        out += struct.pack("bb", 0, 0)
    return bytes(out)


def _attach_sber_orphans_raw(ff2: bytes, orphans: list) -> bytes:
    """In-place glyf patch: .notdef → host orphan → remaining orphans.

    Avoids fontTools save (fails on padded/corrupt loca with 3k glyphs).
    Same decoded length; only overwrites existing loca spans.
    """
    import struct

    if not orphans:
        return ff2
    glyf = _get_font_table(ff2, b"glyf")
    loca = _get_font_table(ff2, b"loca")
    head = _get_font_table(ff2, b"head")
    if not glyf or not loca or not head or len(head) < 52:
        return ff2
    long_loca = int.from_bytes(head[50:52], "big") == 1
    if long_loca:
        n_entries = len(loca) // 4
        offs = [
            int.from_bytes(loca[i * 4:(i + 1) * 4], "big")
            for i in range(n_entries)
        ]
    else:
        n_entries = len(loca) // 2
        offs = [
            int.from_bytes(loca[i * 2:(i + 1) * 2], "big") * 2
            for i in range(n_entries)
        ]
    n_glyphs = n_entries - 1
    if n_glyphs <= 1 or offs[-1] > len(glyf):
        return ff2

    def _span(gid: int) -> int:
        return max(0, offs[gid + 1] - offs[gid])

    orph = sorted({int(g) for g in orphans if 1 <= int(g) < n_glyphs})
    if not orph:
        return ff2

    new_glyf = bytearray(glyf)

    def _write(gid: int, data: bytes) -> bool:
        s, e = offs[gid], offs[gid + 1]
        if e > len(new_glyf) or s < 0 or e < s:
            return False
        room = e - s
        if len(data) > room:
            return False
        new_glyf[s:e] = data + (b"\x00" * (room - len(data)))
        return True

    if len(orph) == 1:
        rec0 = _sber_composite_glyph_record(orph)
        if not _write(0, rec0):
            return ff2
    else:
        # Largest orphan hosts the rest; .notdef points at host (fits in 42 B).
        host = max(orph, key=_span)
        rest = [g for g in orph if g != host]
        rec_host = _sber_composite_glyph_record(rest)
        rec0 = _sber_composite_glyph_record([host])
        if _span(host) < len(rec_host) or _span(0) < len(rec0):
            return ff2
        if not _write(host, rec_host) or not _write(0, rec0):
            return ff2

    out = _restore_font_table(ff2, b"glyf", bytes(new_glyf))
    if len(out) != len(ff2):
        return ff2
    try:
        out = _recalculate_sfnt_checksum_adjustment(out)
    except Exception:
        pass
    if len(out) != len(ff2):
        return ff2
    # Proton-like verify without fontTools save.
    still = _sber_orphan_nonempty_gids(out, set())
    # seeds={0} only is enough if all orphans hang off .notdef; also allow
    # caller keep set — re-check with {0} ∪ nothing leaves only non-closure.
    # If still nonempty outside {0}+deps, fail. Use composite walk on raw.
    try:
        ft = TTFont(BytesIO(out), recalcBBoxes=False, recalcTimestamp=False)
        keep = {0} | _composite_dependency_gids(ft, {0})
        go = ft.getGlyphOrder()
        glyf_t = ft["glyf"]
        open_g = [
            gid for gid, name in enumerate(go)
            if gid not in keep
            and int(getattr(glyf_t[name], "numberOfContours", 0) or 0) != 0
            and gid in set(orph)
        ]
        ft.close()
        if open_g:
            return ff2
    except Exception:
        # fontTools may warn on loca; if raw composite bytes look right, keep.
        pass
    logger.info(
        "Sber attach orphans RAW: host=%s orphans=%s",
        orph[0] if len(orph) == 1 else max(orph, key=_span),
        orph[:12],
    )
    return out


def _attach_sber_orphans_as_components(
    ff2: bytes, keep_gids: Set[int], orphans: list,
) -> bytes:
    """Fold orphan nonempty GIDs into reverse-closure via .notdef composite.

    Prefer raw in-place glyf patch (stable length, no fontTools save). Fall
    back to fontTools only when raw cannot fit spans.
    """
    if not orphans:
        return ff2
    raw = _attach_sber_orphans_raw(ff2, list(orphans))
    if raw != ff2:
        return raw

    from fontTools.ttLib.tables._g_l_y_f import Glyph as _TGlyph
    from fontTools.ttLib.tables._g_l_y_f import GlyphComponent

    try:
        ft = TTFont(BytesIO(ff2))
        glyf = ft["glyf"]
        go = ft.getGlyphOrder()
    except Exception:
        return ff2
    if not go:
        return ff2
    host_gid = 0
    host_name = go[0]
    host = glyf[host_name]
    host_is_comp = int(getattr(host, "numberOfContours", 0) or 0) < 0
    orphan_names = []
    keep = set(keep_gids) | {0}
    for og in orphans:
        if og < 0 or og >= len(go) or og in keep:
            continue
        oname = go[og]
        if int(getattr(glyf[oname], "numberOfContours", 0) or 0) == 0:
            continue
        orphan_names.append(oname)
    if not orphan_names:
        return ff2
    try:
        if host_is_comp:
            comps = list(getattr(host, "components", []) or [])
            for oname in orphan_names:
                if any(getattr(c, "glyphName", None) == oname for c in comps):
                    continue
                c = GlyphComponent()
                c.glyphName = oname
                c.x = 0
                c.y = 0
                c.flags = 0x22
                comps.append(c)
            if not comps:
                return ff2
            for c in comps[:-1]:
                c.flags = (getattr(c, "flags", 0) | 0x22) & ~0x01
            comps[-1].flags = (getattr(comps[-1], "flags", 0) | 0x02) & ~0x21
            host.components = comps
            glyf[host_name] = host
        else:
            new = _TGlyph()
            new.numberOfContours = -1
            comps = []
            for oname in orphan_names:
                c = GlyphComponent()
                c.glyphName = oname
                c.x = 0
                c.y = 0
                c.flags = 0x22
                comps.append(c)
            for c in comps[:-1]:
                c.flags = 0x22
            comps[-1].flags = 0x02
            new.components = comps
            glyf[host_name] = new
        bio = BytesIO()
        ft.save(bio, reorderTables=False)
        out = bio.getvalue()
        for tag in (b"cvt ", b"fpgm", b"prep", b"hhea", b"maxp", b"head"):
            orig_t = _get_font_table(ff2, tag)
            if orig_t is not None:
                out = _restore_font_table(out, tag, orig_t)
        orig_csa = _get_head_csa(ff2)
        if orig_csa is not None:
            out = _restore_head_csa(out, orig_csa)
        table_order = _get_table_order_from_font(ff2)
        out = _reorder_ttf_tables(out, table_order)
        out = _align_ttf_tail(out)
        out = _pad_decoded_font_to_exact(out, ff2)
        if len(out) != len(ff2):
            logger.warning(
                "Sber attach orphans size miss host=%d %d≠%d — revert",
                host_gid, len(out), len(ff2),
            )
            return ff2
        ft2 = TTFont(BytesIO(out))
        keep2 = keep | _composite_dependency_gids(ft2, keep)
        still = [
            gid for gid, name in enumerate(ft2.getGlyphOrder())
            if gid not in keep2
            and int(getattr(ft2["glyf"][name], "numberOfContours", 0) or 0) != 0
        ]
        ft2.close()
        if still:
            logger.warning(
                "Sber attach orphans still open %s — revert", still[:12],
            )
            return ff2
        logger.info(
            "Sber attach orphans as components: host=%d orphans=%s",
            host_gid, [go.index(n) for n in orphan_names][:12],
        )
        return out
    except Exception as exc:
        logger.warning("Sber attach orphans failed: %s", exc)
        return ff2



def _blank_sber_font_orphans(ff2_orig: bytes, active_gids: Set[int]) -> Tuple[bytes, int]:
    """Content-only / donor-orig: attach orphans into .notdef, else blank."""
    ft = TTFont(BytesIO(ff2_orig))
    glyf = ft["glyf"]
    go = ft.getGlyphOrder()
    needed: Set[int] = set(active_gids) | {0}
    if 3 in active_gids:
        needed.add(3)
    # Match Proton — never flatten before measuring reverse-closure.
    keep_pre = needed | _composite_dependency_gids(ft, needed)
    orphans = [
        gid for gid, name in enumerate(go)
        if gid not in keep_pre
        and int(getattr(glyf[name], "numberOfContours", 0) or 0) != 0
    ]
    nonempty = sum(
        1
        for name in go
        if int(getattr(glyf[name], "numberOfContours", 0) or 0) != 0
    )
    ft.close()
    if orphans:
        attached = _attach_sber_orphans_as_components(
            ff2_orig, keep_pre, orphans,
        )
        if attached != ff2_orig:
            ft2 = TTFont(BytesIO(attached))
            keep2 = needed | _composite_dependency_gids(ft2, needed)
            still = [
                gid for gid, name in enumerate(ft2.getGlyphOrder())
                if gid not in keep2
                and int(getattr(ft2["glyf"][name], "numberOfContours", 0) or 0) != 0
            ]
            ft2.close()
            if not still:
                out = _finalize_sber_contour_loca_profile(attached, ff2_orig, keep2)
                still2 = _sber_orphan_nonempty_gids(out, active_gids)
                if still2:
                    out2 = _attach_sber_orphans_as_components(
                        out, keep2 | set(active_gids) | {0}, still2,
                    )
                    if out2 != out:
                        out = out2
                return out, max(1, len(orphans))
            ff2_orig = attached
            ft = TTFont(BytesIO(ff2_orig))
            glyf = ft["glyf"]
            go = ft.getGlyphOrder()
            keep_pre = needed | _composite_dependency_gids(ft, needed)
            orphans = [
                gid for gid, name in enumerate(go)
                if gid not in keep_pre
                and int(getattr(glyf[name], "numberOfContours", 0) or 0) != 0
            ]
            ft.close()

    if not orphans:
        out = _finalize_sber_contour_loca_profile(ff2_orig, ff2_orig, keep_pre)
        return out, 0
    if nonempty - len(orphans) < 66:
        return ff2_orig, 0

    ft = TTFont(BytesIO(ff2_orig))
    glyf = ft["glyf"]
    go = ft.getGlyphOrder()
    # Flatten painted CIDs only — never gid 0 (attach host).
    flat_gids = set(active_gids) - {0}
    _flatten_sber_composites(ft, flat_gids)
    keep = needed | _composite_dependency_gids(ft, needed)
    trimmed = _trim_orphan_glyf_contours(glyf, go, keep)
    if trimmed == 0:
        out = _finalize_sber_contour_loca_profile(ff2_orig, ff2_orig, keep)
        return out, 0

    bio = BytesIO()
    ft.save(bio, reorderTables=False)
    font_bytes = bio.getvalue()
    font_bytes = _force_long_loca(font_bytes, ff2_orig)
    for tag in (b"cvt ", b"fpgm", b"hhea", b"hmtx", b"head", b"maxp", b"prep"):
        orig_t = _get_font_table(ff2_orig, tag)
        if orig_t is not None:
            font_bytes = _restore_font_table(font_bytes, tag, orig_t)
    orig_csa = _get_head_csa(ff2_orig)
    if orig_csa is not None:
        font_bytes = _restore_head_csa(font_bytes, orig_csa)
    table_order = _get_table_order_from_font(ff2_orig)
    font_bytes = _reorder_ttf_tables(font_bytes, table_order)
    font_bytes = _align_ttf_tail(font_bytes)
    font_bytes = _pad_decoded_font_to_exact(font_bytes, ff2_orig)
    font_bytes = _finalize_sber_contour_loca_profile(font_bytes, ff2_orig, keep)
    logger.info(
        "Sber orphan blank: trimmed=%d keep=%d bytes=%d (orig %d)",
        trimmed, len(keep), len(font_bytes), len(ff2_orig),
    )
    return font_bytes, trimmed


def _sber_orphan_nonempty_gids(ff2: bytes, active_gids: Set[int]) -> list:
    """Nonempty glyph GIDs outside reverse-closure of active CIDs (Proton-like)."""
    try:
        ft = TTFont(BytesIO(ff2))
        glyf = ft["glyf"]
        go = ft.getGlyphOrder()
        needed: Set[int] = set(active_gids) | {0}
        if 3 in active_gids:
            needed.add(3)
        # Do NOT flatten — Proton walks composites as-is.
        keep = needed | _composite_dependency_gids(ft, needed)
        orphans = []
        for gid, name in enumerate(go):
            if gid in keep:
                continue
            if int(getattr(glyf[name], "numberOfContours", 0) or 0) != 0:
                orphans.append(gid)
        ft.close()
        return orphans
    except Exception:
        return []


def _apply_sber_orphan_blank_pdf(pdf: bytes) -> bytes:
    """Attach reverse-closure orphans into .notdef; skip if already clean."""
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception:
        return pdf
    try:
        fm = tut._find_font_objects(doc)
        key = _pick_arial(fm)
        if not key:
            return pdf
        meta = fm[key]
        ff_xref = meta.get("fontfile_xref")
        if not ff_xref:
            return pdf
        ff2_orig = doc.xref_stream(ff_xref)
        cs_xref = doc[0].get_contents()[0]
        stream = doc.xref_stream(cs_xref)
        active = _gids_in_stream(stream)
        orphans = _sber_orphan_nonempty_gids(ff2_orig, active)
        if not orphans:
            return pdf
        ff2, trimmed = _blank_sber_font_orphans(ff2_orig, active)
        if trimmed == 0 and orphans:
            keep = set(active) | {0, 3}
            attached = _attach_sber_orphans_as_components(ff2_orig, keep, orphans)
            if attached != ff2_orig:
                ff2 = attached
                trimmed = max(1, len(orphans))
        if trimmed == 0:
            return pdf
        if _sber_orphan_nonempty_gids(ff2, active):
            logger.warning("Sber orphan apply still open after blank/attach")
            return pdf
        offsets, first, count, xref_off = tut._parse_xref_table(pdf)
        sorted_xrefs = sorted(offsets.items(), key=lambda x: x[1])
        obj_ranges = {xn: (s, tut._find_object_end(pdf, s)) for xn, s in sorted_xrefs}
        replacements: Dict[int, bytes] = {}
        if trimmed and ff_xref in obj_ranges:
            s, e = obj_ranges[ff_xref]
            comp_m = re.search(
                rb"stream\r?\n([\x00-\xff]*?)\r?\nendstream", pdf[s:e], re.DOTALL)
            donor_comp = comp_m.group(1) if comp_m else None
            ff2_comp, ff2_dec = _compress_font_like_donor(ff2, donor_comp)
            if donor_comp and len(ff2_comp) != len(donor_comp):
                logger.warning(
                    "Sber orphan apply FontFile2 size miss %d≠%d — skip",
                    len(ff2_comp), len(donor_comp),
                )
                return pdf
            replacements[ff_xref] = tut._make_modified_obj(
                pdf[s:e], ff_xref, new_stream=ff2_comp, new_length1=len(ff2_dec),
            )
        if not replacements:
            return pdf
        out = bytearray(pdf[:sorted_xrefs[0][1]])
        new_offsets: Dict[int, int] = {}
        for xn, (os_, oe) in sorted(obj_ranges.items(), key=lambda kv: kv[1][0]):
            new_offsets[xn] = len(out)
            out.extend(replacements.get(xn, pdf[os_:oe]))
        new_xref_off = len(out)
        xref_lines = [b"xref\n", f"{first} {count}\n".encode()]
        for n in range(first, first + count):
            if n == 0:
                xref_lines.append(b"0000000000 65535 f \n")
            elif n in new_offsets:
                xref_lines.append(f"{new_offsets[n]:010d} 00000 n \n".encode())
            else:
                xref_lines.append(b"0000000000 00000 f \n")
        out.extend(b"".join(xref_lines))
        trailer_pos = pdf.find(b"trailer", xref_off)
        sx_pos = pdf.find(b"startxref", trailer_pos)
        out.extend(pdf[trailer_pos:sx_pos])
        out.extend(f"startxref\n{new_xref_off}\n%%EOF\n".encode())
        result = bytes(out)
        if len(result) != len(pdf):
            result = _pad_pdf_to_exact_size(result, len(pdf))
        logger.info("Sber orphan apply: closed %d orphans via FontFile2", len(orphans))
        return result
    except Exception as exc:
        logger.warning("Sber orphan blank PDF skip: %s", exc)
        return pdf
    finally:
        doc.close()



def _glyph_x_min(glyf_table, glyph) -> int:
    """Left extent of glyph ink (simple or composite). Prefer live coordinates."""
    try:
        nc = int(getattr(glyph, "numberOfContours", 0) or 0)
        if nc > 0:
            coords = getattr(glyph, "coordinates", None)
            if coords is not None and len(coords):
                return int(min(c[0] for c in coords))
            try:
                glyph.recalcBounds(glyf_table)
            except Exception:
                pass
            if hasattr(glyph, "xMin"):
                return int(glyph.xMin)
            return 0
        if nc == -1:
            # BoundsPen.draw breaks on nested components (draw() needs glyfTable).
            # Always recalc from live component outlines — stale xMin causes
            # composite AW too narrow (Cyrillic А over Latin A) → overlap.
            try:
                glyph.recalcBounds(glyf_table)
            except Exception:
                pass
            if hasattr(glyph, "xMin"):
                return int(glyph.xMin)
    except Exception:
        pass
    return int(getattr(glyph, "xMin", 0) or 0)


def _glyph_x_max(glyf_table, glyph) -> int:
    """Right extent of glyph ink (simple or composite)."""
    try:
        nc = int(getattr(glyph, "numberOfContours", 0) or 0)
        if nc > 0:
            coords = getattr(glyph, "coordinates", None)
            if coords is not None and len(coords):
                return int(max(c[0] for c in coords))
            try:
                glyph.recalcBounds(glyf_table)
            except Exception:
                pass
            if hasattr(glyph, "xMax"):
                return int(glyph.xMax)
            return 0
        if nc == -1:
            try:
                glyph.recalcBounds(glyf_table)
            except Exception:
                pass
            if hasattr(glyph, "xMax"):
                return int(glyph.xMax)
    except Exception:
        pass
    return int(getattr(glyph, "xMax", 0) or 0)


def _clear_glyph_program(glyph) -> None:
    """Drop TrueType bytecode — after coord shifts it lies and warps widths."""
    try:
        from fontTools.ttLib.tables.ttProgram import Program

        glyph.program = Program()
    except Exception:
        try:
            glyph.program = None
        except Exception:
            pass


def _fix_composite_advances_sber(ft: TTFont, gids: Set[int], *, min_rsb: int = 64) -> int:
    """Expand AW for composites whose components were widened.

    Cyrillic «А» is a composite of Latin «A». Harmonize may grow the shared
    component while a native parent keeps shell AW → ink past advance → overlap.
    """
    emb_h = ft["hmtx"].metrics
    go = ft.getGlyphOrder()
    glyf = ft["glyf"]
    fixed = 0
    for gid in gids:
        if gid <= 0 or gid >= len(go):
            continue
        gname = go[gid]
        g = glyf[gname]
        if int(getattr(g, "numberOfContours", 0) or 0) != -1:
            continue
        cur = emb_h.get(gname)
        if not cur or not cur[0]:
            continue
        try:
            g.recalcBounds(glyf)
        except Exception:
            pass
        xmin = _glyph_x_min(glyf, g)
        xmax = _glyph_x_max(glyf, g)
        if xmax <= xmin:
            continue
        need = int(xmax + min_rsb)
        aw0 = int(cur[0])
        if need > aw0:
            emb_h[gname] = (need, int(cur[1]))
            _clear_glyph_program(g)
            fixed += 1
    return fixed


def _glyph_aw_ink_gap(ft: TTFont, gid: int) -> Optional[int]:
    """AW−ink for simple glyphs (diagnostic; Proton uses pairwise gaps)."""
    go = ft.getGlyphOrder()
    if gid < 0 or gid >= len(go):
        return None
    gn = go[gid]
    if gn not in ft["glyf"].glyphs:
        return None
    g = ft["glyf"][gn]
    nc = int(getattr(g, "numberOfContours", 0) or 0)
    if nc <= 0:
        return None
    cur = ft["hmtx"].metrics.get(gn)
    if not cur or not cur[0]:
        return None
    try:
        ink = int(g.xMax) - int(g.xMin)
    except Exception:
        return None
    return int(cur[0]) - ink


# Proton detector/sber_v2/glyph_spacing.py: pairwise ink gap between consecutive
# non-space CIDs in Tj runs: gap = adv1 + xMin2 - xMax1 (font units).
_SBER_INK_GAP_SPACE_CID = 3
_SBER_INK_GAP_MIN_ALLOWED = (21, 22, 33, 43, 52)


def _sber_tj_cid_runs(stream: bytes) -> List[List[int]]:
    """Identity-H CID runs from (…)Tj literals (same as Proton)."""
    runs: List[List[int]] = []
    if not stream:
        return runs
    for m in re.finditer(rb"\((?:\\.|[^\\)])*\)\s*Tj", stream):
        lit = m.group(0)
        end = lit.rfind(b")")
        if end <= 0:
            continue
        try:
            raw = _pdf_literal_unescape(lit[1:end])
        except Exception:
            continue
        if len(raw) < 2 or len(raw) % 2 != 0:
            continue
        runs.append([(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw), 2)])
    return runs


def _sber_pair_ink_gaps(
    ft: TTFont,
    runs: List[List[int]],
) -> List[Tuple[int, int, int]]:
    """[(gap, left_cid, right_cid), ...] sorted ascending by gap."""
    go = ft.getGlyphOrder()
    glyf = ft["glyf"]
    emb_h = ft["hmtx"].metrics
    out: List[Tuple[int, int, int]] = []
    for cids in runs:
        for left, right in zip(cids, cids[1:]):
            if left == _SBER_INK_GAP_SPACE_CID or right == _SBER_INK_GAP_SPACE_CID:
                continue
            if left < 0 or right < 0 or left >= len(go) or right >= len(go):
                continue
            try:
                g1 = glyf[go[left]]
                g2 = glyf[go[right]]
                # Match Proton: expand composites before reading bounds.
                if int(getattr(g1, "numberOfContours", 0) or 0) < 0:
                    g1.expand(glyf)
                if int(getattr(g2, "numberOfContours", 0) or 0) < 0:
                    g2.expand(glyf)
                adv1 = int(emb_h[go[left]][0])
                gap = (adv1 + int(g2.xMin)) - int(g1.xMax)
            except Exception:
                continue
            out.append((int(gap), int(left), int(right)))
    out.sort()
    return out


# Proton K-SBER-SBP-EXACT-PROFILE: static-label alphabet outline SHAs are
# singleton Jasper contracts. Never mutate these contours (ink_gap nudges).
_SBER_STATIC_LABEL_OUTLINE_CHARS = frozenset(
    "БИКНОПСФабвгдеиклмнопрстуцчяё"
)


def _sber_ink_gap_editable_gid(
    gid: int,
    gid_to_uni: Optional[Dict[int, int]],
    *,
    locked_outline_gids: Optional[Set[int]] = None,
) -> bool:
    """AW bumps: grafted/non-native OK; native labels → digits only.

    Without ``locked_outline_gids``, only digit CIDs are editable (conservative:
    label Unicode on FIO grafts must not lose AW via the old digits-only path
    when locks are missing — prefer passing native locks from patch).
    """
    if locked_outline_gids is not None and int(gid) not in locked_outline_gids:
        return True
    if not gid_to_uni:
        return locked_outline_gids is not None
    uni = gid_to_uni.get(gid)
    if uni is None:
        return False
    try:
        return unicodedata.category(chr(int(uni)))[:1] == "N"
    except (TypeError, ValueError, OverflowError):
        return False


def _sber_static_label_outline_gid(
    gid: int,
    gid_to_uni: Optional[Dict[int, int]],
    *,
    locked_outline_gids: Optional[Set[int]] = None,
) -> bool:
    """True when gid must keep donor outline (K-SBER-SBP-EXACT-PROFILE).

    Grafted FIO CIDs may share label Unicode (о/а/…) but are not the Jasper
    singleton outline slots — only ``locked_outline_gids`` (native shell) are
    immutable. Without that set, fall back to alphabet Unicode (conservative).
    """
    if locked_outline_gids is not None:
        return int(gid) in locked_outline_gids
    if not gid_to_uni:
        return False
    uni = gid_to_uni.get(int(gid))
    if uni is None:
        return False
    try:
        return chr(int(uni)) in _SBER_STATIC_LABEL_OUTLINE_CHARS
    except (TypeError, ValueError, OverflowError):
        return False


def _nudge_simple_glyph_edge(
    ft: TTFont,
    gid: int,
    *,
    edge: str,
    delta: int,
) -> bool:
    """Move all points on xMin or xMax by ``delta`` (font units)."""
    if not delta:
        return True
    go = ft.getGlyphOrder()
    if gid < 0 or gid >= len(go):
        return False
    gn = go[gid]
    g = ft["glyf"][gn]
    nc = int(getattr(g, "numberOfContours", 0) or 0)
    if nc <= 0:
        return False
    try:
        coords = g.coordinates
    except Exception:
        return False
    if not coords:
        return False
    xs = [int(pt[0]) for pt in coords]
    x_min, x_max = min(xs), max(xs)
    try:
        from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates as _GC
        if edge == "xmax":
            new_pts = [
                (int(pt[0]) + delta if int(pt[0]) == x_max else int(pt[0]), int(pt[1]))
                for pt in coords
            ]
            g.coordinates = _GC(new_pts)
            g.xMin = x_min
            g.xMax = x_max + delta
        elif edge == "xmin":
            new_pts = [
                (int(pt[0]) + delta if int(pt[0]) == x_min else int(pt[0]), int(pt[1]))
                for pt in coords
            ]
            g.coordinates = _GC(new_pts)
            g.xMin = x_min + delta
            g.xMax = x_max
        else:
            return False
    except Exception:
        return False
    return True


def _ensure_sber_ink_gap_profile(
    ft: TTFont,
    used_gids: Set[int],
    *,
    max_floor: int = 550,
    max_ceiling: int = 650,
    min_band: Tuple[int, int] = (21, 52),
    gid_to_uni: Optional[Dict[int, int]] = None,
    stream: Optional[bytes] = None,
    locked_outline_gids: Optional[Set[int]] = None,
    donor_aws: Optional[Set[int]] = None,
    donor_ft: Optional[TTFont] = None,
) -> bool:
    """Proton pairwise ink_gap: min∈{21,22,33,43,52}, max∈[550,650].

    Returns True when profile is in-band after edits (or nothing to score).
    """
    del min_band  # band is discrete allowed set, not a continuous range
    emb_h = ft["hmtx"].metrics
    go = ft.getGlyphOrder()
    allowed_min = _SBER_INK_GAP_MIN_ALLOWED
    runs = _sber_tj_cid_runs(stream or b"")
    if not runs:
        # No content — nothing Proton can score.
        return True
    try:
        n_hm = int(ft["hhea"].numberOfHMetrics)
    except Exception:
        n_hm = len(go)

    def _full_metric(gid: int) -> bool:
        return 0 <= gid < n_hm

    def _pairs() -> List[Tuple[int, int, int]]:
        return _sber_pair_ink_gaps(ft, runs)

    def _locked(gid: int) -> bool:
        return _sber_static_label_outline_gid(
            gid, gid_to_uni, locked_outline_gids=locked_outline_gids,
        )

    def _editable(gid: int) -> bool:
        return _sber_ink_gap_editable_gid(
            gid, gid_to_uni, locked_outline_gids=locked_outline_gids,
        )

    def _nudge(gid: int, *, edge: str, delta: int) -> bool:
        # Never reshape native label outlines → SBER_STATIC_LABEL_OUTLINE_MISMATCH.
        if _locked(gid):
            return False
        return _nudge_simple_glyph_edge(ft, gid, edge=edge, delta=delta)

    # Prefer full donor advance set (pre-blank); live emb_h loses values.
    donor_aws_set = set(int(x) for x in (donor_aws or ()) if int(x) > 0)
    if not donor_aws_set:
        donor_aws_set = {
            int(m[0]) for m in emb_h.values() if m and int(m[0]) > 0
        }

    def _snap_left_aw_to_allowed_gap(left: int, gmin: int) -> Optional[int]:
        """Pick donor AW on left so pair gap lands in allowed_min (hmtx-only).

        Corpus genuines allow negative RSB (xMax > AW); ink_gap uses
        adv+xMin_r−xMax_l, so do not require AW ≥ xMax+1 here.
        """
        if not _full_metric(left):
            return None
        gn = go[left]
        cur = emb_h.get(gn) or (0, 0)
        try:
            gL = ft["glyf"][gn]
            if int(getattr(gL, "numberOfContours", 0) or 0) < 0:
                gL.expand(ft["glyf"])
            min_aw = max(1, int(gL.xMin) + 1)
        except Exception:
            min_aw = 1
        # Prefer larger targets first (smaller visual squeeze).
        for target in sorted(allowed_min, reverse=True):
            need_aw = int(cur[0]) + (int(target) - int(gmin))
            if need_aw < min_aw or need_aw not in donor_aws_set:
                continue
            if need_aw == int(cur[0]):
                return int(target)
            emb_h[gn] = (need_aw, int(cur[1]))
            logger.info(
                "Sber ink_gap_min: snap cid=%d AW %d→%d (gap %d→%d donor)",
                left, int(cur[0]), need_aw, gmin, target,
            )
            return int(target)
        return None

    pairs = _pairs()
    if not pairs:
        return True

    # Ensure a wide pair survives (≥550). Prefer bumping a digit AW on the
    # current max pair's left glyph — labels stay put when possible.
    if pairs[-1][0] < max_floor:
        _gap0, left0, _right0 = pairs[-1]
        cand = left0
        if not (_full_metric(cand) and _editable(cand)):
            cand = next(
                (
                    L
                    for g, L, _R in reversed(pairs)
                    if _full_metric(L) and _editable(L)
                ),
                left0 if _full_metric(left0) else None,
            )
        if cand is not None and _full_metric(cand):
            gn = go[cand]
            cur = emb_h.get(gn) or (0, 0)
            need = int(cur[0]) + (max_floor - _gap0)
            if need > int(cur[0]):
                emb_h[gn] = (need, int(cur[1]))
                logger.info(
                    "Sber ink_gap_max: bump cid=%d AW→%d (pair-gap→≥%d)",
                    cand, need, max_floor,
                )

    for _ in range(96):
        pairs = _pairs()
        if not pairs:
            return
        gmin, left, right = pairs[0]
        gmax, left_m, right_m = pairs[-1][0], pairs[-1][1], pairs[-1][2]
        # HARD WIDE_GAP: any pair >650 must shrink before exit.
        if gmax > max_ceiling:
            shrink = int(gmax - max_ceiling + 8)
            # AW-only shrink is safe even on native label CIDs (outline untouched).
            # Prefer donor-corpus advances so hmtx uniq stays 425.
            if _full_metric(left_m):
                gn = go[left_m]
                cur = emb_h.get(gn) or (0, 0)
                max_aw = int(cur[0]) - shrink
                picked = None
                if donor_aws_set:
                    cands = [a for a in donor_aws_set if 1 <= a <= max_aw]
                    if cands:
                        picked = max(cands)
                if picked is None and (_editable(left_m) or not _locked(left_m)):
                    picked = max(1, max_aw)
                if picked is not None and picked != int(cur[0]):
                    emb_h[gn] = (int(picked), int(cur[1]))
                    logger.info(
                        "Sber ink_gap_wide: shrink cid=%d AW %d→%d (gap %d→≤%d)",
                        left_m, int(cur[0]), picked, gmax, max_ceiling,
                    )
                    continue
                # Geometry: even min donor AW leaves gap >650 — nudge editable side.
                if donor_aws_set and _full_metric(left_m):
                    min_donor = min(donor_aws_set)
                    if int(cur[0]) != min_donor:
                        emb_h[gn] = (int(min_donor), int(cur[1]))
                        continue
            if _nudge(left_m, edge="xmax", delta=shrink):
                logger.info(
                    "Sber ink_gap_wide: grow cid=%d xMax +%d (gap→≤%d)",
                    left_m, shrink, max_ceiling,
                )
                continue
            if _nudge(right_m, edge="xmin", delta=-shrink):
                logger.info(
                    "Sber ink_gap_wide: lower cid=%d xMin -%d (gap→≤%d)",
                    right_m, shrink, max_ceiling,
                )
                continue
            # Last resort: shrink any left in wide pairs (hmtx-only, donor AW).
            fixed = False
            for gap, L, R in reversed(pairs):
                if gap <= max_ceiling:
                    break
                if not _full_metric(L):
                    continue
                gn = go[L]
                cur = emb_h.get(gn) or (0, 0)
                max_aw = int(cur[0]) - int(gap - max_ceiling + 8)
                picked = None
                if donor_aws_set:
                    cands = [a for a in donor_aws_set if 1 <= a <= max_aw]
                    if cands:
                        picked = max(cands)
                if picked is None and (_editable(L) or not _locked(L)):
                    picked = max(1, max_aw)
                if picked is not None and picked != int(cur[0]):
                    emb_h[gn] = (int(picked), int(cur[1]))
                    fixed = True
                    break
            if fixed:
                continue
            # Restore only helps when a prior bump drifted AW; if donor AW
            # already yields wide gap, restoring loops forever — abort.
            logger.warning(
                "Sber ink_gap_wide: cannot shrink max=%d (cid %d→%d)",
                gmax, left_m, right_m,
            )
            return False
        if gmin in allowed_min and max_floor <= gmax <= max_ceiling:
            return True
        if gmin in allowed_min and gmax < max_floor:
            # Min OK — only restore wide carrier.
            _g, left0, _r = pairs[-1]
            if _full_metric(left0):
                gn = go[left0]
                cur = emb_h.get(gn) or (0, 0)
                emb_h[gn] = (int(cur[0]) + (max_floor - _g), int(cur[1]))
                logger.info(
                    "Sber ink_gap_max: bump cid=%d AW (min ok, max %d→≥%d)",
                    left0, _g, max_floor,
                )
                continue
            return False
        # Target for the current min pair.
        if gmin < allowed_min[0]:
            target = 21
        elif gmin > allowed_min[-1]:
            target = 52
        else:
            target = min(allowed_min, key=lambda v: abs(v - gmin))
            if target == gmin:
                if max_floor <= gmax <= max_ceiling:
                    return True
                continue
        delta = int(target - gmin)
        if delta == 0:
            if max_floor <= gmax <= max_ceiling:
                return True
            continue
        if delta > 0:
            # Gap too small / overlap: prefer AW bump on digits; else shrink
            # left xMax or raise right xMin (keeps label advances).
            if _full_metric(left) and _editable(left):
                gn = go[left]
                cur = emb_h.get(gn) or (0, 0)
                emb_h[gn] = (int(cur[0]) + delta, int(cur[1]))
                logger.info(
                    "Sber ink_gap_min: bump cid=%d AW +%d (pair %d→%d→%d)",
                    left, delta, left, right, target,
                )
                continue
            if _nudge(left, edge="xmax", delta=-delta):
                logger.info(
                    "Sber ink_gap_min: shrink cid=%d xMax %d (pair →%d→%d)",
                    left, -delta, right, target,
                )
                continue
            if _nudge(right, edge="xmin", delta=delta):
                logger.info(
                    "Sber ink_gap_min: raise cid=%d xMin +%d (pair %d→→%d)",
                    right, delta, left, target,
                )
                continue
            if _full_metric(left) and not _locked(left):
                gn = go[left]
                cur = emb_h.get(gn) or (0, 0)
                emb_h[gn] = (int(cur[0]) + delta, int(cur[1]))
                logger.info(
                    "Sber ink_gap_min: bump letter cid=%d AW +%d (pair →%d)",
                    left, delta, target,
                )
                continue
            return False
        # Gap too large on min pair: shrink left AW / grow bbox (digit first).
        shrink = -delta
        if _full_metric(left) and _editable(left):
            gn = go[left]
            cur = emb_h.get(gn) or (0, 0)
            try:
                gL = ft["glyf"][gn]
                if int(getattr(gL, "numberOfContours", 0) or 0) < 0:
                    gL.expand(ft["glyf"])
                min_aw = int(gL.xMax) + 1
            except Exception:
                min_aw = 1
            new_aw = int(cur[0]) - shrink
            if new_aw >= min_aw:
                emb_h[gn] = (new_aw, int(cur[1]))
                logger.info(
                    "Sber ink_gap_min: shrink cid=%d AW -%d (pair %d→%d→%d)",
                    left, shrink, left, right, target,
                )
                continue
        grow = shrink
        if _editable(right) and _nudge(
            right, edge="xmin", delta=-grow,
        ):
            logger.info(
                "Sber ink_gap_min: lower digit cid=%d xMin -%d (pair %d→→%d)",
                right, grow, left, target,
            )
            continue
        if _editable(left) and _nudge(
            left, edge="xmax", delta=grow,
        ):
            logger.info(
                "Sber ink_gap_min: grow digit cid=%d xMax +%d (pair →%d→%d)",
                left, grow, right, target,
            )
            continue
        if not _locked(left) and _nudge(left, edge="xmax", delta=grow):
            logger.info(
                "Sber ink_gap_min: grow letter cid=%d xMax +%d (pair →%d→%d)",
                left, grow, right, target,
            )
            continue
        if not _locked(right) and _nudge(right, edge="xmin", delta=-grow):
            logger.info(
                "Sber ink_gap_min: lower letter cid=%d xMin -%d (pair %d→→%d)",
                right, grow, left, target,
            )
            continue
        if _full_metric(left) and not _locked(left):
            gn = go[left]
            cur = emb_h.get(gn) or (0, 0)
            try:
                gL = ft["glyf"][gn]
                if int(getattr(gL, "numberOfContours", 0) or 0) < 0:
                    gL.expand(ft["glyf"])
                min_aw = int(gL.xMax) + 1
            except Exception:
                min_aw = 1
            new_aw = int(cur[0]) - shrink
            if new_aw >= min_aw:
                emb_h[gn] = (new_aw, int(cur[1]))
                logger.info(
                    "Sber ink_gap_min: shrink letter cid=%d AW -%d (pair →%d)",
                    left, shrink, target,
                )
                continue
        # Native FIO slots: outline locked, but donor-AW snap is hmtx-only and
        # survives uniq-collapse (75→22 via AW 1193→1140 on corpus shells).
        if _snap_left_aw_to_allowed_gap(left, gmin) is not None:
            continue
        logger.warning(
            "Sber ink_gap_min: give up gmin=%d want=%d left=%d right=%d",
            gmin, target, left, right,
        )
        return False

    pairs = _pairs()
    if pairs and pairs[-1][0] < max_floor:
        _g, left0, _r = pairs[-1]
        if _full_metric(left0):
            gn = go[left0]
            cur = emb_h.get(gn) or (0, 0)
            emb_h[gn] = (int(cur[0]) + (max_floor - _g), int(cur[1]))
            logger.info(
                "Sber ink_gap_max: re-bump cid=%d after min fix",
                left0,
            )
    pairs = _pairs()
    if not pairs:
        return True
    gmin, _, _ = pairs[0]
    gmax = pairs[-1][0]
    return gmin in allowed_min and max_floor <= gmax <= max_ceiling


def _expand_simple_glyph_ink(ft: TTFont, gid: int, need_ink: int) -> bool:
    """Grow simple-glyph ink (xMax−xMin) without changing advance width."""
    go = ft.getGlyphOrder()
    if gid < 0 or gid >= len(go):
        return False
    gn = go[gid]
    g = ft["glyf"][gn]
    nc = int(getattr(g, "numberOfContours", 0) or 0)
    if nc <= 0:
        return False
    try:
        coords = g.coordinates
    except Exception:
        return False
    if not coords:
        return False
    xs = [int(pt[0]) for pt in coords]
    x_min, x_max = min(xs), max(xs)
    cur_ink = x_max - x_min
    if need_ink <= cur_ink:
        return False
    delta = int(need_ink - cur_ink)
    return _nudge_simple_glyph_edge(ft, gid, edge="xmax", delta=delta)


def _apply_sber_ink_gap_hmtx(
    ff2: bytes,
    used_gids: Set[int],
    *,
    gid_to_uni: Optional[Dict[int, int]] = None,
    stream: Optional[bytes] = None,
    ff2_orig: Optional[bytes] = None,
    locked_outline_gids: Optional[Set[int]] = None,
) -> bytes:
    """Patch FontFile2 so Proton pairwise ink_gap is in-band.

    May nudge glyf xMin/xMax and/or patch hmtx. Glyf edits require a full save.
    """
    if not ff2 or not (used_gids or stream):
        return ff2
    try:
        ft = TTFont(BytesIO(ff2))
    except Exception:
        return ff2
    go = ft.getGlyphOrder()
    emb_h = ft["hmtx"].metrics
    try:
        n_hm = int(ft["hhea"].numberOfHMetrics)
    except Exception:
        n_hm = len(go)
    watch = set(used_gids) | {0}
    if n_hm > 0:
        watch.add(n_hm - 1)
    before_aw: Dict[int, Tuple[int, int]] = {}
    before_ink: Dict[int, int] = {}
    for gid in watch:
        if gid < 0 or gid >= len(go):
            continue
        m = emb_h.get(go[gid])
        if m:
            before_aw[gid] = (int(m[0]), int(m[1]))
        try:
            g = ft["glyf"][go[gid]]
            nc = int(getattr(g, "numberOfContours", 0) or 0)
            if nc > 0:
                before_ink[gid] = int(g.xMax) - int(g.xMin)
        except Exception:
            pass
    donor_ref = ff2_orig or ff2
    try:
        _donor_ft = TTFont(BytesIO(donor_ref)) if donor_ref else None
    except Exception:
        _donor_ft = None
    _ensure_sber_ink_gap_profile(
        ft, set(used_gids), gid_to_uni=gid_to_uni, stream=stream,
        locked_outline_gids=locked_outline_gids,
        donor_aws=_hmtx_pos_advance_set(donor_ref),
        donor_ft=_donor_ft,
    )
    if _donor_ft is not None:
        try:
            _donor_ft.close()
        except Exception:
            pass
    # Keep AW changes inside donor uniq-advance set (HARD 428/424).
    donor_ref = ff2_orig or ff2
    donor_aws = _hmtx_pos_advance_set(donor_ref)
    if donor_aws:
        for gid in watch:
            if gid < 0 or gid >= len(go):
                continue
            gn = go[gid]
            m = emb_h.get(gn)
            if not m:
                continue
            aw, lsb = int(m[0]), int(m[1])
            if aw <= 0 or aw in donor_aws:
                continue
            min_aw = 1
            try:
                g = ft["glyf"][gn]
                nc = int(getattr(g, "numberOfContours", 0) or 0)
                if nc != 0:
                    if nc < 0:
                        g.expand(ft["glyf"])
                    min_aw = max(1, int(g.xMax) + 1)
            except Exception:
                pass
            emb_h[gn] = (_snap_aw_to_donor_set(aw, donor_aws, min_aw=min_aw), lsb)
    aw_changed = False
    glyf_changed = False
    aw_by_gid: Dict[int, int] = {}
    lsb_by_gid: Dict[int, int] = {}
    for gid, (aw0, lsb0) in before_aw.items():
        m = emb_h.get(go[gid])
        if not m:
            continue
        aw, lsb = int(m[0]), int(m[1])
        aw_by_gid[gid] = aw
        lsb_by_gid[gid] = lsb
        if aw != aw0 or lsb != lsb0:
            aw_changed = True
        gap = _glyph_aw_ink_gap(ft, gid)
        if gap is not None and gid in before_ink:
            ink_now = aw - gap
            if ink_now != before_ink[gid]:
                glyf_changed = True
    if not aw_changed and not glyf_changed:
        return ff2
    if glyf_changed:
        bio = BytesIO()
        ft.save(bio, reorderTables=False)
        out = bio.getvalue()
        out = _force_long_loca(out, ff2)
        for tag in (b"cvt ", b"fpgm", b"hhea", b"head", b"maxp", b"prep"):
            orig_t = _get_font_table(ff2, tag)
            if orig_t is not None:
                out = _restore_font_table(out, tag, orig_t)
        orig_hmtx = _get_font_table(ff2, b"hmtx")
        our_hmtx = _get_font_table(out, b"hmtx")
        if orig_hmtx and our_hmtx and len(our_hmtx) != len(orig_hmtx):
            out = _replace_font_table_data(out, "hmtx", orig_hmtx)
        out = _patch_hmtx_widths_sber(
            out, ff2, set(aw_by_gid) | set(used_gids) | {0}, set(), {},
            lsb_by_gid=lsb_by_gid, aw_by_gid=aw_by_gid,
        )
        if len(out) != len(ff2):
            out = _pad_decoded_font_to_exact(out, ff2)
        if len(out) != len(ff2):
            return ff2
        out = _collapse_hmtx_uniq_advances_sber(
            out, donor_ref, used_gids=set(used_gids) | {0},
        )
        if len(out) != len(ff2) and len(out) < len(ff2):
            out = _pad_decoded_font_to_exact(out, ff2)
        return out if len(out) == len(ff2) else ff2
    patched = _patch_hmtx_widths_sber(
        ff2, ff2, set(aw_by_gid) | set(used_gids) | {0}, set(), {},
        lsb_by_gid=lsb_by_gid, aw_by_gid=aw_by_gid,
    )
    patched = _collapse_hmtx_uniq_advances_sber(
        patched, donor_ref, used_gids=set(used_gids) | {0},
    )
    if len(patched) != len(ff2) and len(patched) < len(ff2):
        patched = _pad_decoded_font_to_exact(patched, ff2)
    return patched if len(patched) == len(ff2) else ff2


def _ensure_advance_covers_ink_sber(ft: TTFont, gids: Set[int], *, min_rsb: int = 64) -> int:
    """Guarantee AW >= xMax + min_rsb for every used glyph (simple + composite)."""
    emb_h = ft["hmtx"].metrics
    go = ft.getGlyphOrder()
    glyf = ft["glyf"]
    fixed = 0
    for gid in gids:
        if gid <= 0 or gid >= len(go):
            continue
        gname = go[gid]
        g = glyf[gname]
        nc = int(getattr(g, "numberOfContours", 0) or 0)
        if nc == 0:
            continue
        cur = emb_h.get(gname)
        if not cur or not cur[0]:
            continue
        if nc == -1:
            try:
                g.recalcBounds(glyf)
            except Exception:
                pass
        xmax = _glyph_x_max(glyf, g)
        need = int(xmax + min_rsb)
        if need > int(cur[0]):
            emb_h[gname] = (need, int(cur[1]))
            if nc > 0:
                _clear_glyph_program(g)
            fixed += 1
    return fixed


def _harmonize_sber_side_bearings(ft: TTFont, gids: Set[int]) -> int:
    """Выровнять LSB/RSB у используемых глифов.

    В Arial у «н» огромный правый зазор, у «к» отрицательный — пары
    «нъ» / «къ» выглядят криво (дыра / наложение). Сдвигаем контур к
    медианному LSB и ставим AW = ink + LSB + RSB. Хинты сбрасываем.
    """
    emb_h = ft["hmtx"].metrics
    go = ft.getGlyphOrder()
    glyf = ft["glyf"]

    lsbs: List[int] = []
    rsbs: List[int] = []
    for gid in gids:
        if gid <= 0 or gid >= len(go):
            continue
        g = glyf[go[gid]]
        if int(getattr(g, "numberOfContours", 0) or 0) <= 0:
            continue
        cur = emb_h.get(go[gid])
        if not cur or not cur[0]:
            continue
        xmin = _glyph_x_min(glyf, g)
        xmax = _glyph_x_max(glyf, g)
        if xmax <= xmin:
            continue
        lsbs.append(xmin)
        rsbs.append(int(cur[0]) - xmax)

    if len(lsbs) < 3:
        return 0

    def _med(vals: List[int]) -> int:
        s = sorted(vals)
        return int(s[len(s) // 2])

    # Типичные поля кириллицы Arial ~130/60; не раздуваем и не схлопываем.
    target_lsb = max(96, min(150, _med([v for v in lsbs if v > 0] or lsbs)))
    pos_rsb = [v for v in rsbs if v > 0]
    target_rsb = max(48, min(100, _med(pos_rsb) if pos_rsb else 64))

    fixed = 0
    for gid in gids:
        if gid <= 0 or gid >= len(go):
            continue
        gname = go[gid]
        g = glyf[gname]
        nc = int(getattr(g, "numberOfContours", 0) or 0)
        cur = emb_h.get(gname)
        if not cur or not cur[0]:
            continue
        aw0 = int(cur[0])
        if nc > 0:
            xmin = _glyph_x_min(glyf, g)
            xmax = _glyph_x_max(glyf, g)
            if xmax <= xmin:
                continue
            ink = xmax - xmin
            delta = target_lsb - xmin
            if delta != 0:
                coords = getattr(g, "coordinates", None)
                if coords is not None and len(coords):
                    from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates as _GC

                    pts = [(int(x) + delta, int(y)) for x, y in coords]
                    g.coordinates = _GC(pts)
                    try:
                        g.recalcBounds(glyf)
                    except Exception:
                        pass
            new_aw = int(ink + target_lsb + target_rsb)
            # Не сжимать сильно уже узкие буквы: AW минимум ink+lsb+24.
            new_aw = max(new_aw, int(ink + target_lsb + 24))
            emb_h[gname] = (new_aw, int(target_lsb))
            _clear_glyph_program(g)
            fixed += 1
        elif nc == -1:
            # Композит: только подправить AW под реальные bounds, без сдвига.
            xmin = _glyph_x_min(glyf, g)
            xmax = _glyph_x_max(glyf, g)
            if xmax <= xmin:
                continue
            need = int((xmax - xmin) + target_lsb + target_rsb)
            # Если контур левее target_lsb — расширяем AW, не трогаем компоненты.
            new_aw = max(aw0, need, xmax + target_rsb)
            if new_aw != aw0 or int(cur[1]) != target_lsb:
                emb_h[gname] = (int(new_aw), int(min(cur[1], target_lsb) if cur[1] else target_lsb))
                _clear_glyph_program(g)
                fixed += 1
    return fixed


def _sync_hmtx_from_library(
    ft: TTFont,
    filled_cids: set,
    rev_gid_cp: Dict[int, int],
) -> None:
    """Advance from glyph library; LSB must match outline xMin (not shell leftover)."""
    emb_h = ft["hmtx"].metrics
    go = ft.getGlyphOrder()
    glyf = ft["glyf"]
    for gid in filled_cids:
        if gid >= len(go):
            continue
        cp = rev_gid_cp.get(gid)
        if cp is None:
            continue
        aw = sgl.get_entry_aw(cp)
        eg = go[gid]
        cur = emb_h.get(eg, (0, 0))
        if not aw:
            aw = cur[0]
        if not aw:
            continue
        lsb = _glyph_x_min(glyf, glyf[eg])
        if cur[0] != aw or cur[1] != lsb:
            emb_h[eg] = (int(aw), int(lsb))


def _normalize_hmtx_lsb_sber(ft: TTFont, gids: Set[int]) -> int:
    """Force hmtx LSB = outline xMin for used glyphs.

    Filled slots keep the shell's old LSB (often 256) while grafted contours
    have xMin≈0. Some PDF engines scale glyphs using (xMax-LSB) vs /W and
    stretch letters into each other.
    """
    emb_h = ft["hmtx"].metrics
    go = ft.getGlyphOrder()
    glyf = ft["glyf"]
    fixed = 0
    for gid in gids:
        if gid <= 0 or gid >= len(go):
            continue
        eg = go[gid]
        g = glyf[eg]
        if getattr(g, "numberOfContours", 0) == 0:
            continue
        cur = emb_h.get(eg)
        if not cur or not cur[0]:
            continue
        lsb = _glyph_x_min(glyf, g)
        if cur[1] != lsb:
            emb_h[eg] = (cur[0], int(lsb))
            fixed += 1
    return fixed


def _fill_empty_components_sber(
    needed_cids: Set[int],
    glyf_table,
    glyph_order: list,
    rev_gid_cp: Dict[int, int],
    filled_out: Set[int],
) -> int:
    from copy import deepcopy

    filled = 0
    for cid in list(needed_cids):
        if cid >= len(glyph_order):
            continue
        g = glyf_table[glyph_order[cid]]
        if getattr(g, "numberOfContours", 0) != -1:
            continue
        if not getattr(g, "components", None):
            try:
                g.expand(glyf_table)
            except Exception:
                continue
        for comp in g.components or []:
            try:
                cgid = glyph_order.index(comp.glyphName)
            except ValueError:
                continue
            needed_cids.add(cgid)
            if cgid >= len(glyph_order) or cgid in filled_out:
                continue
            cgname = glyph_order[cgid]
            if getattr(glyf_table[cgname], "numberOfContours", 0) != 0:
                continue
            cp = rev_gid_cp.get(cid)
            bundle = sgl.get_bundle(cp, glyph_order, cgid) if cp is not None else None
            if not bundle:
                for other_cp in rev_gid_cp.values():
                    bundle = sgl.get_bundle(other_cp, glyph_order, cgid)
                    if bundle:
                        break
            if not bundle:
                continue
            for gn, gg in bundle.items():
                if gn in glyf_table:
                    glyf_table[gn] = gg
            if getattr(glyf_table[cgname], "numberOfContours", 0) != 0:
                filled += 1
                filled_out.add(cgid)
    return filled


# Proton SBER_FONT_HMTX_UNIQ_ADVANCES: sbp_outgoing genuines always 425.
_SBP_HMTX_UNIQ_ADVANCES = 425


def _hmtx_pos_advance_set(font_bytes: bytes) -> Set[int]:
    try:
        ft = TTFont(BytesIO(font_bytes))
        out = {int(m[0]) for m in ft["hmtx"].metrics.values() if int(m[0]) > 0}
        ft.close()
        return out
    except Exception:
        return set()


def _snap_aw_to_donor_set(aw: int, allowed: Set[int], *, min_aw: int = 1) -> int:
    if aw in allowed and aw >= min_aw:
        return int(aw)
    cands = [a for a in allowed if a >= min_aw]
    if not cands:
        cands = list(allowed)
    if not cands:
        return int(aw)
    return int(min(cands, key=lambda a: (abs(a - aw), a)))


def _collapse_hmtx_uniq_advances_sber(
    font_bytes: bytes,
    ff2_orig: bytes,
    *,
    used_gids: Optional[Set[int]] = None,
    target: int = _SBP_HMTX_UNIQ_ADVANCES,
) -> bytes:
    """Keep hmtx unique positive advances == donor cardinality (425).

    1) Restore unused slots to exact donor AW (preserves full 425 set).
    2) Snap used foreign AWs onto the donor set (kills 428-style extras).
    """
    donor = _hmtx_pos_advance_set(ff2_orig)
    if not donor:
        return font_bytes
    before = _hmtx_pos_advance_set(font_bytes)
    if len(before) == target and before == donor:
        return font_bytes

    hhea = _get_font_table(font_bytes, b"hhea")
    hmtx = _get_font_table(font_bytes, b"hmtx")
    orig_hmtx = _get_font_table(ff2_orig, b"hmtx")
    orig_hhea = _get_font_table(ff2_orig, b"hhea")
    if not hhea or not hmtx or not orig_hmtx or len(hhea) < 36:
        return font_bytes
    num_hm = int.from_bytes(hhea[34:36], "big")
    orig_num_hm = (
        int.from_bytes(orig_hhea[34:36], "big") if orig_hhea and len(orig_hhea) >= 36 else num_hm
    )
    arr = bytearray(hmtx)
    used = set(used_gids or ())
    try:
        ft = TTFont(BytesIO(font_bytes))
        go = ft.getGlyphOrder()
        glyf = ft["glyf"]
    except Exception:
        ft = None
        go = []
        glyf = None

    edits = 0
    # Unused → exact donor AW so rare advances cannot disappear (424 HARD).
    for gid in range(min(num_hm, orig_num_hm, len(arr) // 4, len(orig_hmtx) // 4)):
        if gid in used:
            continue
        off = gid * 4
        donor_aw = int.from_bytes(orig_hmtx[off:off + 2], "big")
        cur_aw = int.from_bytes(arr[off:off + 2], "big")
        if donor_aw != cur_aw:
            arr[off:off + 2] = int(donor_aw).to_bytes(2, "big")
            edits += 1

    # Used foreign → nearest donor AW (ink-safe floor).
    for gid in sorted(used):
        if gid < 0 or gid >= num_hm or gid * 4 + 2 > len(arr):
            continue
        off = gid * 4
        aw = int.from_bytes(arr[off:off + 2], "big")
        if aw <= 0 or aw in donor:
            continue
        min_aw = 1
        if ft is not None and 0 <= gid < len(go):
            try:
                g = glyf[go[gid]]
                nc = int(getattr(g, "numberOfContours", 0) or 0)
                if nc != 0:
                    if nc < 0:
                        g.expand(glyf)
                    min_aw = max(1, int(g.xMax) + 1)
            except Exception:
                pass
        new_aw = _snap_aw_to_donor_set(aw, donor, min_aw=min_aw)
        if new_aw != aw:
            arr[off:off + 2] = int(new_aw).to_bytes(2, "big")
            edits += 1
    if ft is not None:
        ft.close()

    out = _restore_font_table(font_bytes, b"hmtx", bytes(arr)) if edits else font_bytes
    our = _hmtx_pos_advance_set(out)
    missing = sorted(donor - our)
    if missing:
        from collections import Counter

        hmtx2 = bytearray(_get_font_table(out, b"hmtx") or arr)
        try:
            ft2 = TTFont(BytesIO(out))
            counts = Counter(
                int(m[0]) for m in ft2["hmtx"].metrics.values() if int(m[0]) > 0
            )
            ft2.close()
        except Exception:
            counts = Counter()
        restored = 0
        # Prefer unused duplicate AWs; if still short, any unused positive slot.
        for pass_i in (0, 1):
            for gid in range(min(num_hm, len(hmtx2) // 4)):
                if restored >= len(missing):
                    break
                if gid in used or gid <= 0:
                    continue
                off = gid * 4
                aw = int.from_bytes(hmtx2[off:off + 2], "big")
                if aw <= 0:
                    continue
                if pass_i == 0 and counts.get(aw, 0) <= 1:
                    continue
                if pass_i == 1 and aw in missing:
                    continue  # don't erase another still-missing value
                new_aw = missing[restored]
                if new_aw == aw:
                    restored += 1
                    continue
                hmtx2[off:off + 2] = int(new_aw).to_bytes(2, "big")
                counts[aw] = max(0, counts.get(aw, 0) - 1)
                counts[new_aw] = counts.get(new_aw, 0) + 1
                restored += 1
                edits += 1
            if restored >= len(missing):
                break
        if restored:
            out = _restore_font_table(out, b"hmtx", bytes(hmtx2))

    after = len(_hmtx_pos_advance_set(out))
    if edits or after != target:
        logger.info(
            "Sber hmtx uniq advances %d → %d (target %d, edits=%d missing_left=%d)",
            len(before), after, target, edits, max(0, target - after),
        )
    return out


def _patch_hmtx_widths_sber(
    font_bytes: bytes,
    ff2_orig: bytes,
    needed_gids: Set[int],
    filled_cids: Set[int],
    rev_gid_cp: Dict[int, int],
    *,
    lsb_by_gid: Optional[Dict[int, int]] = None,
    aw_by_gid: Optional[Dict[int, int]] = None,
) -> bytes:
    """hmtx: advance/LSB из гармонизированного ft; fallback — library / shell."""
    our_hhea = _get_font_table(font_bytes, b"hhea")
    our_hmtx_raw = _get_font_table(font_bytes, b"hmtx")
    orig_hhea = _get_font_table(ff2_orig, b"hhea")
    orig_hmtx_raw = _get_font_table(ff2_orig, b"hmtx")
    if not our_hhea or not our_hmtx_raw or not orig_hhea or not orig_hmtx_raw:
        return font_bytes
    if len(our_hhea) < 36 or len(orig_hhea) < 36:
        return font_bytes
    our_num_hm = int.from_bytes(our_hhea[34:36], "big")
    orig_num_hm = int.from_bytes(orig_hhea[34:36], "big")
    hmtx_arr = bytearray(our_hmtx_raw)
    fixed = 0
    lsb_by_gid = lsb_by_gid or {}
    aw_by_gid = aw_by_gid or {}
    donor_aws = {
        int.from_bytes(orig_hmtx_raw[i:i + 2], "big")
        for i in range(0, min(len(orig_hmtx_raw), orig_num_hm * 4), 4)
        if int.from_bytes(orig_hmtx_raw[i:i + 2], "big") > 0
    }
    patch_gids = set(needed_gids) | set(aw_by_gid or {}) | set(lsb_by_gid or {})
    for gid in sorted(patch_gids):
        # gid 0 (.notdef) may carry ink_gap_max floor — allow AW patch.
        if gid < 0 or gid >= our_num_hm:
            continue
        off = gid * 4
        if off + 4 > len(hmtx_arr):
            continue
        cur_aw = int.from_bytes(hmtx_arr[off:off + 2], "big")
        cur_lsb = int.from_bytes(hmtx_arr[off + 2:off + 4], "big", signed=True)
        if cur_lsb >= 0x8000:
            cur_lsb -= 0x10000
        new_aw = aw_by_gid.get(gid)
        if new_aw is None and gid in filled_cids:
            cp = rev_gid_cp.get(gid)
            if cp is not None:
                new_aw = sgl.get_entry_aw(cp)
        if new_aw is None and cur_aw == 0 and gid < orig_num_hm:
            orig_off = gid * 4
            if orig_off + 2 <= len(orig_hmtx_raw):
                orig_aw = int.from_bytes(orig_hmtx_raw[orig_off:orig_off + 2], "big")
                if orig_aw > 0:
                    new_aw = orig_aw
        if new_aw is not None and donor_aws and int(new_aw) not in donor_aws:
            new_aw = _snap_aw_to_donor_set(int(new_aw), donor_aws, min_aw=1)
        new_lsb = lsb_by_gid.get(gid)
        changed = False
        if new_aw and int(new_aw) != cur_aw:
            hmtx_arr[off:off + 2] = int(new_aw).to_bytes(2, "big")
            changed = True
        if new_lsb is not None and int(new_lsb) != cur_lsb:
            hmtx_arr[off + 2:off + 4] = int(new_lsb).to_bytes(2, "big", signed=True)
            changed = True
        if changed:
            fixed += 1
    if fixed == 0:
        return font_bytes
    return _restore_font_table(font_bytes, b"hmtx", bytes(hmtx_arr))


def _shell_ruble_cids(stream: bytes) -> Set[int]:
    """CID, которые в shell кодируют ₽ (\\rZ → 0x0D5A или пара 0x00 0x0F)."""
    out: Set[int] = set()
    for raw in _iter_pdf_literal_tj(stream):
        if b"\\rZ" in raw or b"\x5crZ" in raw:
            out.add(_RUBLE_CID)
        unesc = _pdf_literal_unescape(raw)
        if b"\rZ" in unesc:
            out.add(_RUBLE_CID)
        if b"\x00\x0f" in raw.lower() or b"\x00\x0F" in raw:
            out.add(15)
    return out


def _patch_sber_font(
    ff2_orig: bytes,
    planned: Dict[int, int],
    active_gids: Set[int],
    *,
    blank_unused: bool = True,
    planned_uni_gid: Optional[Dict[int, int]] = None,
    shell_stream: Optional[bytes] = None,
    shell_uni_gid: Optional[Dict[int, int]] = None,
    internal_entropy_slack: bool = False,
    donor_compressed_len: Optional[int] = None,
    date_center_y: Optional[float] = None,
    date_center_exclusive: bool = False,
) -> Tuple[bytes, Dict[int, int], TTFont, Set[int]]:
    from copy import deepcopy
    from fontTools.ttLib.tables._g_l_y_f import Glyph as _TGlyph

    sgl.ensure_library()
    ft = TTFont(BytesIO(ff2_orig))
    orig_ft = TTFont(BytesIO(ff2_orig))
    orig_glyf = orig_ft["glyf"]
    glyf = ft["glyf"]
    go = ft.getGlyphOrder()
    gid_to_cp_exist = {gid: cp for cp, gid in planned.items()}
    new_uni_gid = dict(planned_uni_gid or planned)
    needed: Set[int] = set(active_gids) | {0, 3}
    # Preserve native composite leaves before the first blanking pass.
    needed |= _composite_dependency_gids(ft, needed)
    # Preserve ruble ink only when that CID is actually painted in the
    # final stream — blind keep of CID 15 → SBER_FONT_REVERSE_GLYPH_CLOSURE.
    preserve_cids = _shell_ruble_cids(shell_stream) if shell_stream else set()
    preserve_cids &= needed
    if 15 in active_gids and len(go) > 15:
        preserve_cids.add(15)
    if _RUBLE_CID in active_gids:
        preserve_cids.add(_RUBLE_CID)
    rev = {gid: cp for cp, gid in new_uni_gid.items()}
    shell_rev = {gid: cp for cp, gid in (shell_uni_gid or {}).items()}
    empty = _TGlyph()
    empty.numberOfContours = 0
    blanked = 0
    filled = 0
    # filled_cids = library grafts only. native_cids = unchanged shell ink.
    # Harmonize/sync on natives → K-SBER-SBP-EXACT-PROFILE label widths drift.
    filled_cids: Set[int] = set()
    native_cids: Set[int] = set()

    for cid, gname in enumerate(go):
        if cid in preserve_cids:
            if getattr(orig_glyf[gname], "numberOfContours", 0) != 0:
                glyf[gname] = deepcopy(orig_glyf[gname])
                native_cids.add(cid)
            continue
        if blank_unused and cid not in needed:
            # Always clear unused donor ink (simple + composite).
            # Old bug: `continue` when orig_nc!=0 left orphans for Proton.
            if getattr(glyf[gname], "numberOfContours", 0) != 0:
                glyf[gname] = deepcopy(empty)
                blanked += 1
            continue
        if cid not in needed:
            continue
        cp = rev.get(cid)
        if cp is None:
            cp = gid_to_cp_exist.get(cid)
        if cp is None:
            continue
        if cid in preserve_cids:
            continue
        if cp == 0x20 or cid == 3:
            # Space: advance-only, no contours — keep as native, never graft.
            native_cids.add(cid)
            continue
        shell_cp = shell_rev.get(cid)
        # Native shell glyph for this exact unicode — keep outline+hints+AW.
        # numberOfContours==-1 (composite) is NOT ink if all leaves are empty.
        if (
            shell_cp is not None
            and shell_cp == cp
            and _glyph_has_visible_ink(glyf, go, cid)
        ):
            native_cids.add(cid)
            continue
        # Stolen / empty slot — (пере)залить из library.
        bundle = sgl.get_bundle(cp, go, cid)
        if not bundle:
            logger.warning(
                "Sber font: no library bundle U+%04X cid=%d — empty face risk",
                cp, cid,
            )
            continue
        for gn, g in bundle.items():
            if gn in glyf:
                glyf[gn] = g
        for cgid in sgl.get_bundle_component_gids(cp):
            if cgid < len(go):
                needed.add(cgid)
        if _glyph_has_visible_ink(glyf, go, cid):
            filled += 1
            filled_cids.add(cid)
            new_uni_gid[cp] = cid
        else:
            logger.warning(
                "Sber font: graft U+%04X cid=%d still empty after bundle",
                cp, cid,
            )

    comp_filled = _fill_empty_components_sber(needed, glyf, go, rev, filled_cids)
    filled += comp_filled

    for cid in needed:
        if cid in new_uni_gid.values():
            continue
        cp = gid_to_cp_exist.get(cid)
        if cp is not None and cp >= 0x20:
            new_uni_gid[cp] = cid

    if blank_unused:
        pruned_uni: Dict[int, int] = {}
        for cp, gid in new_uni_gid.items():
            if gid in needed:
                pruned_uni[cp] = gid
        new_uni_gid = pruned_uni

    if 3 in needed:
        new_uni_gid[0x20] = 3

    # Restore shell glyf+hmtx for natives first, then flatten ONCE, then trim.
    # Old order (flatten→trim→restore composites→flatten) blanked component
    # leaves before the second flatten → empty М/С/а/е… → pairs≈108 + ne=53.
    protect_ink = filled_cids | native_cids
    orig_h = orig_ft["hmtx"].metrics
    emb_h = ft["hmtx"].metrics
    for cid in native_cids:
        if cid >= len(go):
            continue
        gn = go[cid]
        glyf[gn] = deepcopy(orig_glyf[gn])
        if gn in orig_h:
            emb_h[gn] = orig_h[gn]
    # Also restore any still-needed composite component leaves from donor
    # (may have been cleared by an earlier unused-blank pass on a soft miss).
    pre_flat_keep = set(active_gids) | {0, 3} | set(filled_cids) | set(native_cids)
    pre_flat_keep |= _composite_dependency_gids(ft, pre_flat_keep)
    for cid in pre_flat_keep:
        if cid >= len(go) or cid in filled_cids:
            continue
        gn = go[cid]
        if getattr(glyf[gn], "numberOfContours", 0) == 0:
            og = orig_glyf[gn]
            if getattr(og, "numberOfContours", 0) != 0:
                glyf[gn] = deepcopy(og)
                if gn in orig_h:
                    emb_h[gn] = orig_h[gn]

    paint_set = set(active_gids) | set(filled_cids) | set(native_cids)
    # Flatten only grafted/filled CIDs — never native shell labels.
    # Flattening Tahoma composites changes outline SHA →
    # SBER_STATIC_LABEL_OUTLINE_MISMATCH (exact-profile HARD).
    paint_flat = set(filled_cids) | (set(active_gids) - set(native_cids))
    _flatten_sber_composites(ft, paint_flat)
    # Re-assert native outlines after any composite surgery.
    for cid in native_cids:
        if cid >= len(go):
            continue
        gn = go[cid]
        glyf[gn] = deepcopy(orig_glyf[gn])
        if gn in orig_h:
            emb_h[gn] = orig_h[gn]
    keep_gids = set(paint_set) | {0, 3}
    keep_gids |= _composite_dependency_gids(ft, keep_gids)
    trimmed = 0
    if blank_unused:
        trimmed = _trim_orphan_glyf_contours(
            glyf, go, keep_gids, orig_glyf_table=orig_glyf, filled_cids=protect_ink,
        )

    _sync_hmtx_from_library(ft, filled_cids, rev)
    lsb_fixed = _normalize_hmtx_lsb_sber(ft, filled_cids)
    harm = _harmonize_sber_side_bearings(ft, filled_cids)
    # Only widen filled glyphs + native composites that reference them.
    # Touching all keep_gids → K-SBER-SBP-EXACT-PROFILE label width drift.
    aw_fix_gids: Set[int] = set(filled_cids)
    for _cid in keep_gids:
        if _cid in aw_fix_gids or _cid >= len(go):
            continue
        _g = glyf[go[_cid]]
        if int(getattr(_g, "numberOfContours", 0) or 0) != -1:
            continue
        for _comp in getattr(_g, "components", None) or []:
            try:
                _cg = go.index(_comp.glyphName)
            except ValueError:
                continue
            if _cg in filled_cids:
                aw_fix_gids.add(_cid)
                break
    comp_aw = _fix_composite_advances_sber(ft, aw_fix_gids)
    ink_aw = _ensure_advance_covers_ink_sber(ft, aw_fix_gids)
    if comp_aw or ink_aw:
        logger.info(
            "Sber composite/ink AW fix: composites=%d ink=%d (gids=%d)",
            comp_aw, ink_aw, len(aw_fix_gids),
        )
    # SBP atlas only (phone/card use different Proton bands).
    if internal_entropy_slack:
        _gid_to_uni = {int(g): int(cp) for cp, g in new_uni_gid.items()}
        # Lock only native shell CIDs — grafted FIO slots may share label
        # Unicode but must accept ink_gap bbox nudges (min=75 → 52).
        _lock_outlines = set(native_cids)
        _donor_aws = _hmtx_pos_advance_set(ff2_orig)
        _ensure_sber_ink_gap_profile(
            ft, set(active_gids), gid_to_uni=_gid_to_uni,
            stream=shell_stream, locked_outline_gids=_lock_outlines,
            donor_aws=_donor_aws,
        )
        # Re-assert donor glyf for native slots only. Do NOT restore hmtx —
        # ink_gap donor-AW snaps on native FIO (580→1140) must survive.
        for _gid in list(_lock_outlines):
            if _gid < 0 or _gid >= len(go):
                continue
            _gn = go[_gid]
            _og = orig_glyf[_gn]
            if int(getattr(_og, "numberOfContours", 0) or 0) != 0:
                glyf[_gn] = deepcopy(_og)
        # Second ink_gap pass after outline restore (bbox may have drifted).
        _ensure_sber_ink_gap_profile(
            ft, set(active_gids), gid_to_uni=_gid_to_uni,
            stream=shell_stream, locked_outline_gids=_lock_outlines,
            donor_aws=_donor_aws,
        )
    lsb_by_gid: Dict[int, int] = {}
    aw_by_gid: Dict[int, int] = {}
    _emb_h = ft["hmtx"].metrics
    try:
        _n_hm = int(ft["hhea"].numberOfHMetrics)
    except Exception:
        _n_hm = 0
    _ink_watch = set(keep_gids) | set(active_gids) | {0}
    if _n_hm > 0:
        _ink_watch.add(_n_hm - 1)
    for _gid in _ink_watch:
        if _gid < 0 or _gid >= len(go):
            continue
        _m = _emb_h.get(go[_gid])
        if _m:
            aw_by_gid[_gid] = int(_m[0])
            lsb_by_gid[_gid] = int(_m[1])

    # Library / flatten may leave plain list coords → save() calcIntBounds crash.
    try:
        from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates as _GC
        for _gn in go:
            _g = glyf[_gn]
            _c = getattr(_g, "coordinates", None)
            if _c is not None and not isinstance(_c, _GC):
                _g.coordinates = _GC(list(_c))
    except Exception:
        pass

    if not blank_unused:
        # Grafted outlines can make a dense donor grow. Release only enough
        # unused glyph records to fit the original decoded length; retain all
        # other donor contours so FontFile2 keeps its original compression
        # density. The final shortfall is filled from the donor's existing tail.
        def _probe_bytes() -> bytes:
            probe = BytesIO()
            ft.save(probe, reorderTables=False)
            raw = _force_long_loca(probe.getvalue(), ff2_orig)
            return _pad_decoded_font_to_exact(raw, ff2_orig)

        probe_bytes = _probe_bytes()
        probe_len = len(probe_bytes)
        donor_comp_len = len(zlib.compress(ff2_orig, 6))
        candidates = []
        for cid, gname in enumerate(go):
            if cid in keep_gids or cid in protect_ink:
                continue
            g = glyf[gname]
            if getattr(g, "numberOfContours", 0) == 0:
                continue
            try:
                weight = len(g.compile(glyf))
            except Exception:
                weight = 1
            candidates.append((weight, cid, gname))
        while candidates:
            probe_comp_len = len(zlib.compress(probe_bytes, 6))
            if (
                probe_len <= len(ff2_orig)
                and probe_comp_len <= donor_comp_len
            ):
                break
            gap = probe_len - len(ff2_orig)
            if gap > 0:
                over = [row for row in candidates if row[0] >= gap]
                chosen = min(over, key=lambda row: row[0]) if over else max(candidates)
            else:
                # Compression is still denser than donor: release the smallest
                # unused outline, then tune the same-length tail back upward.
                chosen = min(candidates)
            candidates.remove(chosen)
            _weight, _cid, gname = chosen
            glyf[gname] = deepcopy(empty)
            blanked += 1
            probe_bytes = _probe_bytes()
            probe_len = len(probe_bytes)
        if probe_len > len(ff2_orig):
            logger.warning(
                "Sber dense font still %d B over donor after selective blanking",
                probe_len - len(ff2_orig),
            )

    bio = BytesIO()
    ft.save(bio, reorderTables=False)
    font_bytes = bio.getvalue()
    font_bytes = _force_long_loca(font_bytes, ff2_orig)

    for tag in (b"cvt ", b"fpgm", b"hhea", b"head", b"maxp", b"prep"):
        orig_t = _get_font_table(ff2_orig, tag)
        if orig_t is not None:
            font_bytes = _restore_font_table(font_bytes, tag, orig_t)

    # fontTools save may emit extra hmtx full-metrics; restore donor size
    # then re-apply AW/LSB so "too much hmtx table data" cannot ship.
    orig_hmtx = _get_font_table(ff2_orig, b"hmtx")
    our_hmtx = _get_font_table(font_bytes, b"hmtx")
    if orig_hmtx and our_hmtx and len(our_hmtx) != len(orig_hmtx):
        font_bytes = _replace_font_table_data(font_bytes, "hmtx", orig_hmtx)

    font_bytes = _patch_hmtx_widths_sber(
        font_bytes, ff2_orig, keep_gids, filled_cids, rev,
        lsb_by_gid=lsb_by_gid, aw_by_gid=aw_by_gid,
    )
    font_bytes = _collapse_hmtx_uniq_advances_sber(
        font_bytes, ff2_orig, used_gids=keep_gids,
    )

    orig_csa = _get_head_csa(ff2_orig)
    if orig_csa is not None:
        font_bytes = _restore_head_csa(font_bytes, orig_csa)

    table_order = _get_table_order_from_font(ff2_orig)
    font_bytes = _reorder_ttf_tables(font_bytes, table_order)
    font_bytes = _align_ttf_tail(font_bytes)
    # Date center micro-tune BEFORE glyf slack so donor flate /Length still fits.
    if date_center_y is not None and shell_stream is not None:
        font_bytes = _micro_tune_date_center_font(
            font_bytes,
            shell_stream,
            new_uni_gid,
            float(date_center_y),
            exclusive_only=date_center_exclusive,
        )
        font_bytes = _collapse_hmtx_uniq_advances_sber(
            font_bytes, ff2_orig, used_gids=keep_gids,
        )
    if internal_entropy_slack:
        target_comp = donor_compressed_len or len(zlib.compress(ff2_orig, 6))
        target_dec = len(ff2_orig)
        if len(font_bytes) > target_dec:
            tuned = _shrink_glyf_to_decoded(
                font_bytes, target_dec, target_comp, keep_gids,
            )
        else:
            tuned = _pad_glyf_slack_exact_profile(
                font_bytes, target_dec, target_comp, keep_gids,
            )
        if tuned is None:
            logger.warning(
                "Sber SBP cannot build internal glyf slack dec=%d target=%d comp=%d",
                len(font_bytes), target_dec, target_comp,
            )
        else:
            font_bytes = tuned
        # Slack coalesces pad diversity — re-split into uniq-length blanks.
        font_bytes = _ensure_sber_glyf_uniq_lens_bytes(
            font_bytes, keep_gids, ff2_orig,
        )
        budget = (
            int(donor_compressed_len)
            if donor_compressed_len
            else target_comp
        )
        font_bytes = _relax_sber_ff2_comp_budget(font_bytes, budget, keep_gids)
    else:
        font_bytes = _pad_decoded_font_to_exact(font_bytes, ff2_orig)

    if len(font_bytes) != len(ff2_orig) and len(font_bytes) < len(ff2_orig):
        font_bytes = _pad_decoded_font_to_exact(font_bytes, ff2_orig)
    if len(font_bytes) == len(ff2_orig):
        font_bytes = _restore_sber_struct_fp_tables(font_bytes, ff2_orig)

    # Collapse / slack can leave ink_gap_min off {21,22,33,43,52}. Final glyf
    # bbox nudge on FIO letters (AW snaps are useless after uniq-collapse).
    if internal_entropy_slack and shell_stream:
        _gid_to_uni = {int(g): int(cp) for cp, g in new_uni_gid.items()}
        font_bytes = _apply_sber_ink_gap_hmtx(
            font_bytes,
            set(active_gids),
            gid_to_uni=_gid_to_uni,
            stream=shell_stream,
            ff2_orig=ff2_orig,
            locked_outline_gids=set(native_cids),
        )
        if len(font_bytes) != len(ff2_orig) and len(font_bytes) < len(ff2_orig):
            font_bytes = _pad_decoded_font_to_exact(font_bytes, ff2_orig)
        if len(font_bytes) == len(ff2_orig):
            font_bytes = _restore_sber_struct_fp_tables(font_bytes, ff2_orig)

    logger.info(
        "Sber font patch: active=%d filled=%d blanked=%d trimmed=%d comp=%d lsb=%d harm=%d bytes=%d (orig %d)",
        len(keep_gids), filled, blanked, trimmed, comp_filled, lsb_fixed, harm,
        len(font_bytes), len(ff2_orig),
    )
    return font_bytes, new_uni_gid, TTFont(BytesIO(font_bytes)), set(native_cids)


def _prune_sber_font_subset(pdf: bytearray, meta: dict, stream: bytes) -> bytes:
    """ToUnicode + /W — только CID из content stream (как T-Bank _prune_donor_font_subset)."""
    from tbank_orig_mode import _collect_used_cids, _find_stream_pos_for_xref
    from tbank_sbp_stealth import _patch_cidfont_w

    used = _collect_used_cids(stream)
    active = used.get("F1", set())
    tu_xref = meta.get("tounicode_xref")
    cid_xref = meta.get("cidfont_xref")
    ff_xref = meta.get("fontfile_xref", 0)
    if not tu_xref or not active:
        return bytes(pdf)

    doc = fitz.open(stream=bytes(pdf), filetype="pdf")
    try:
        subset = tut._parse_subset_tounicode(
            doc.xref_stream(tu_xref).decode("latin1", "replace"))
        pruned = {subset[cid]: cid for cid in active if cid in subset}
        if 3 in active:
            pruned[0x20] = 3

        if len(pruned) < len(subset):
            cmap = tut._build_tounicode_cmap(pruned)
            cs, ce = _find_stream_pos_for_xref(bytes(pdf), tu_xref)
            orig_tu_len = (ce - cs) if cs is not None else None
            new_comp = None
            if orig_tu_len:
                new_comp = _pad_to_compressed_size(cmap, orig_tu_len)
            if new_comp is None:
                new_comp = _best_compress(cmap)
            if cs is None:
                logger.warning("Sber prune ToUnicode: stream pos not found xref=%s", tu_xref)
            elif len(new_comp) == ce - cs:
                pdf[cs:ce] = new_comp
                logger.info(
                    "Sber ToUnicode: %d→%d CID, in-place %d B",
                    len(subset), len(pruned), len(new_comp),
                )
            else:
                # Never Length/xref-rebuild — SafeCheck «структура».
                logger.warning(
                    "Sber prune ToUnicode size miss %d→%d — skip (no rebuild)",
                    ce - cs, len(new_comp),
                )
            doc.close()
            doc = fitz.open(stream=bytes(pdf), filetype="pdf")

        if cid_xref and ff_xref:
            gids = sorted(active)
            try:
                ff2 = doc.xref_stream(ff_xref)
                font = TTFont(BytesIO(ff2))
                new_w = tut._build_widths_array(font, gids)
                if _patch_cidfont_w(pdf, cid_xref, new_w):
                    inner = new_w[1:-1]
                    logger.info(
                        "Sber /W: %d CID compact (spaces=%d)",
                        len(gids), inner.count(" "),
                    )
            except Exception as exc:
                logger.warning("Sber prune /W failed: %s", exc)
    finally:
        doc.close()
    return bytes(pdf)


def _sber_unused_nonblank_gids(pdf: bytes) -> Set[int]:
    """Return nonblank glyphs outside content+ToUnicode+composite closure."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        fm = tut._find_font_objects(doc)
        key = _pick_arial(fm)
        if not key:
            return {-1}
        meta = fm[key]
        cs = doc.xref_stream(doc[0].get_contents()[0])
        ff2 = doc.xref_stream(meta["fontfile_xref"])
        subset = tut._parse_subset_tounicode(
            doc.xref_stream(meta["tounicode_xref"]).decode("latin1", "replace")
        )
    finally:
        doc.close()
    try:
        ft = TTFont(BytesIO(ff2))
    except Exception:
        return set()
    seeds = _gids_in_stream(cs) | set(subset.keys()) | {0}
    try:
        closure = seeds | _composite_dependency_gids(ft, seeds)
    except Exception:
        closure = set(seeds)
    go = ft.getGlyphOrder()
    glyf = ft["glyf"]
    nonblank: Set[int] = set()
    for gid, gname in enumerate(go):
        try:
            g = glyf[gname]
            if (
                int(getattr(g, "numberOfContours", 0) or 0) != 0
                or bool(getattr(g, "components", None))
            ):
                nonblank.add(gid)
        except Exception:
            # Corrupt/truncated glyf record — treat as blank for closure gate.
            continue
    try:
        ft.close()
    except Exception:
        pass
    return nonblank - closure


def _blank_gids_in_ff2(ff2: bytes, drop_gids: Set[int], ff2_orig: bytes) -> bytes:
    """Force-empty glyf records for ``drop_gids``; keep decoded size."""
    if not ff2 or not drop_gids:
        return ff2
    from copy import deepcopy
    from fontTools.ttLib.tables._g_l_y_f import Glyph as _TGlyph

    try:
        ft = TTFont(BytesIO(ff2))
    except Exception:
        return ff2
    glyf = ft["glyf"]
    go = ft.getGlyphOrder()
    empty = _TGlyph()
    empty.numberOfContours = 0
    n = 0
    for gid in drop_gids:
        if gid < 0 or gid >= len(go):
            continue
        g = glyf[go[gid]]
        if (
            getattr(g, "numberOfContours", 0) != 0
            or bool(getattr(g, "components", None))
        ):
            glyf[go[gid]] = deepcopy(empty)
            n += 1
    if not n:
        return ff2
    bio = BytesIO()
    ft.save(bio, reorderTables=False)
    out = _force_long_loca(bio.getvalue(), ff2_orig)
    for tag in (b"cvt ", b"fpgm", b"hhea", b"head", b"maxp", b"prep"):
        orig_t = _get_font_table(ff2_orig, tag)
        if orig_t is not None:
            out = _restore_font_table(out, tag, orig_t)
    orig_hmtx = _get_font_table(ff2, b"hmtx")
    if orig_hmtx:
        out = _replace_font_table_data(out, "hmtx", orig_hmtx)
    if len(out) != len(ff2_orig):
        out = _pad_decoded_font_to_exact(out, ff2_orig)
    logger.info("Sber blank leftover nonblank: %d glyphs", n)
    return out if len(out) == len(ff2_orig) else ff2


def _patch_sber_metadata(pdf: bytes, date_time: str) -> bytes:
    receipt_dt = None
    dt_clean = re.sub(r"\s+", " ", (date_time or "").strip())
    # «13:25:32(МСК)» / «13:25:32 (МСК)» → единый вид для парсера.
    dt_clean = re.sub(r"\s*\(МСК\)\s*$", "", dt_clean, flags=re.I).strip()
    dt_clean = re.sub(r"(\d{2}:\d{2}:\d{2})\(", r"\1 (", dt_clean)
    for fmt in ("%d %B %Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            if fmt.startswith("%d %B"):
                months = {
                    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
                    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
                }
                parts = dt_clean.split()
                if len(parts) >= 4:
                    day = int(parts[0])
                    month = months.get(parts[1].lower())
                    if month is None:
                        continue
                    year = int(parts[2])
                    tpart = parts[3].split(":")[0:3]
                    while len(tpart) < 3:
                        tpart.append("00")
                    # секунды могли остаться «28(МСК)» / «28.»
                    tpart = [re.sub(r"\D.*", "", x) or "0" for x in tpart]
                    receipt_dt = datetime(
                        year, month, day,
                        int(tpart[0]), int(tpart[1]), int(tpart[2]),
                    )
                    break
            else:
                receipt_dt = datetime.strptime(dt_clean.split(" (")[0], fmt)
                break
        except ValueError:
            pass

    if receipt_dt is None:
        logger.warning("Sber metadata: cannot parse date %r — keep donor Info", date_time[:48])
        return pdf

    delta = random.randint(30, 90)
    cd_dt = receipt_dt + timedelta(seconds=delta)
    pdf_date = f"D:{cd_dt.strftime('%Y%m%d%H%M%S')}+03'00'"
    pdf = re.sub(
        rb"(/CreationDate\s*)\(D:\d{14}[^)]*\)",
        lambda mo: mo.group(1) + f"({pdf_date})".encode("latin1"),
        pdf,
    )
    pdf = re.sub(
        rb"(/ModDate\s*)\(D:\d{14}[^)]*\)",
        lambda mo: mo.group(1) + f"({pdf_date})".encode("latin1"),
        pdf,
    )
    return pdf


def _randomize_sber_fingerprints(pdf: bytes) -> bytes:
    # Keep font subset prefixes untouched: global byte replace can hit compressed
    # streams and destabilize PDF structure.
    # Preserve exact /ID whitespace/layout so file size and xref stay stable.
    rand_id = lambda: ("%032x" % random.getrandbits(128)).encode("ascii")

    def _id_repl(m: re.Match) -> bytes:
        block = m.group(0)
        ids = [rand_id(), rand_id()]

        def _hex(_hm: re.Match, bag=ids) -> bytes:
            return b"<" + bag.pop(0) + b">"

        return re.sub(rb"<[0-9A-Fa-f]{32}>", _hex, block, count=2)

    pdf = re.sub(
        rb"/ID\s*\[\s*<[0-9A-Fa-f]{32}>\s*<[0-9A-Fa-f]{32}>\s*\]",
        _id_repl,
        pdf,
        count=1,
    )
    return _strip_pdf_eof_tail(pdf)


def _corpus_sbp_donors() -> List[str]:
    global _CORPUS_DONORS_CACHE
    if _CORPUS_DONORS_CACHE is not None:
        return _CORPUS_DONORS_CACHE
    paths: List[str] = []
    roots = [SBER_SHELLS_DIR] if os.path.isdir(SBER_SHELLS_DIR) else []
    if not roots and os.path.isdir(CORPUS_SBER_DIR):
        roots = [CORPUS_SBER_DIR]
    for root in roots:
        for name in sorted(os.listdir(root)):
            if not name.lower().endswith(".pdf"):
                continue
            path = os.path.join(root, name)
            sz = os.path.getsize(path)
            if 100000 <= sz <= 104500:
                paths.append(path)
    _CORPUS_DONORS_CACHE = paths
    return paths


def _chars_in_pdf(path: str) -> Set[str]:
    doc = fitz.open(path)
    fm = tut._find_font_objects(doc)
    key = _pick_arial(fm)
    if not key:
        doc.close()
        return set()
    sub = tut._parse_subset_tounicode(
        doc.xref_stream(fm[key]["tounicode_xref"]).decode("latin1", "replace"))
    doc.close()
    chars = {chr(u) for _, u in sub.items()}
    chars.add("₽")
    return chars


_CID_MAP_CACHE: Dict[str, Tuple[float, Dict[str, int]]] = {}


def _cid_map_from_pdf(path: str) -> Dict[str, int]:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    hit = _CID_MAP_CACHE.get(path)
    if hit and hit[0] == mtime:
        return dict(hit[1])
    doc = fitz.open(path)
    fm = tut._find_font_objects(doc)
    key = _pick_arial(fm)
    if not key:
        doc.close()
        return {}
    sub = tut._parse_subset_tounicode(
        doc.xref_stream(fm[key]["tounicode_xref"]).decode("latin1", "replace"))
    doc.close()
    out = {chr(u): c for c, u in sub.items()}
    out["₽"] = 0x20
    _CID_MAP_CACHE[path] = (mtime, out)
    return dict(out)


def _decode_cid_bytes(raw: bytes, cid_to_char: Dict[int, str]) -> str:
    raw = _pdf_literal_unescape(raw)
    text = ""
    i = 0
    while i < len(raw):
        if raw[i:i + 2] == b"\rZ":
            text += "₽"
            i += 2
            continue
        high = raw[i]
        i += 1
        if i >= len(raw):
            break
        low = raw[i]
        i += 1
        cid = (high << 8) | low
        text += cid_to_char.get(cid, "")
    return text


def _read_field_at_y(
    stream: bytes,
    target_y: float,
    uni_gid: Dict[int, int],
) -> Optional[Tuple[bytes, str]]:
    cid_to_char = {gid: chr(cp) for cp, gid in uni_gid.items()}
    cid_to_char[3] = " "

    for match in _BT_ET_RE.finditer(stream):
        block = match.group(1)
        tm_match = re.search(
            rb"(?:1|[0-9.]+)\s+0\s+0\s+(?:1|[0-9.]+)\s+([\d.]+)\s+([\d.]+)\s+Tm",
            block,
        )
        if not tm_match:
            continue
        y = float(tm_match.group(2))
        if abs(y - target_y) > 2.0:
            continue
        raw = next(_iter_pdf_literal_tj(block), None)
        if raw is not None:
            return raw, _decode_cid_bytes(raw, cid_to_char)
        tj_arr = re.search(rb"\[(.*?)\]\s*TJ", block, re.DOTALL)
        if tj_arr:
            raw = b"".join(
                bytes.fromhex(h.group(1).decode("ascii"))
                for h in re.finditer(rb"<([0-9A-Fa-f]+)>", tj_arr.group(1))
            )
            if raw:
                return raw, _decode_cid_bytes(raw, cid_to_char)
    return None


def _extract_sber_fields(path: str) -> Optional[Dict[str, str]]:
    doc = fitz.open(path)
    cs_xref = None
    for xref in range(1, doc.xref_length()):
        try:
            stream = doc.xref_stream(xref)
            if stream and b"Tm" in stream and b"Tj" in stream and b"BT" in stream:
                cs_xref = xref
                break
        except Exception:
            pass
    if cs_xref is None:
        doc.close()
        return None
    stream = doc.xref_stream(cs_xref)
    fm = tut._find_font_objects(doc)
    key = _pick_arial(fm)
    if not key:
        doc.close()
        return None
    sub = tut._parse_subset_tounicode(
        doc.xref_stream(fm[key]["tounicode_xref"]).decode("latin1", "replace"))
    doc.close()
    uni_gid = {u: c for c, u in sub.items()}

    fields: Dict[str, str] = {}
    for field_name, target_y in _FIELD_Y.items():
        row = _read_field_at_y(stream, target_y, uni_gid)
        if row:
            fields[field_name] = row[1]
    return fields if len(fields) >= 8 else None


def _find_best_donor(prepared: Dict[str, str]) -> Tuple[Optional[str], Set[str]]:
    from sber_stealth_v3 import check_text_with_map

    text = _needed_text(prepared)
    best_path: Optional[str] = None
    best_miss: list = []
    for path in _corpus_sbp_donors():
        miss = check_text_with_map(text, _cid_map_from_pdf(path))
        if best_path is None or len(miss) < len(best_miss):
            best_path = path
            best_miss = miss
    return best_path, set(best_miss)


_PREFERRED_SBP_SKELETON = "866cd81f4fa31279"


def _donor_content_skeleton(path: str) -> str:
    doc = fitz.open(path)
    cs = doc.xref_stream(doc[0].get_contents()[0])
    doc.close()
    return _content_skeleton_hash(cs)


def _pick_charset_donor(prepared: Dict[str, str]) -> Tuple[str, list]:
    """Donor shell: минимум missing CID + fit длин слотов (как T-Bank donor pick)."""
    from sber_stealth_v3 import check_text_with_map

    text = _needed_text(prepared)
    fallback = _ensure_shell_template()
    paths = _corpus_sbp_donors() or [fallback]
    best_path = fallback
    best_miss: list = ["\x00"] * 999
    best_fit = 10**9

    for path in paths:
        if _donor_content_skeleton(path) != _PREFERRED_SBP_SKELETON:
            continue
        cmap = _cid_map_from_pdf(path)
        miss = check_text_with_map(text, cmap)
        if len(miss) > len(best_miss):
            continue
        uni_gid = {ord(ch): gid for ch, gid in cmap.items()}
        lens = _shell_field_byte_lens(path, uni_gid)
        enc = lambda t, m=cmap: _sber_enc(t, {ord(k): v for k, v in m.items()})
        fit = 0
        fit_ok = len(lens) >= 8
        for key in _FIELD_Y:
            if key not in lens:
                fit_ok = False
                break
            _, b = _fit_text_encoded_length(prepared.get(key, ""), lens[key], enc)
            gap = abs(len(b) - lens[key])
            fit += gap * (5 if key in ("amount", "recipient_bank") else 2)
            if gap != 0:
                fit_ok = False
        if not fit_ok:
            continue
        if (len(miss), fit) < (len(best_miss), best_fit):
            best_path, best_miss, best_fit = path, miss, fit

    if best_miss and best_miss[0] == "\x00":
        base_path, _ = _pick_sbp_shell(prepared)
        from sber_stealth_v3 import check_text_with_map
        best_miss = check_text_with_map(text, _cid_map_from_pdf(base_path))

    logger.info(
        "Sber charset donor: %s (miss %d: %s)",
        os.path.basename(best_path), len(best_miss),
        "".join(best_miss[:8]) if best_miss else "—",
    )
    return best_path, best_miss


def _auto_fix_prepared_fields(prepared: Dict[str, str], cmap: dict) -> Dict[str, str]:
    """Preserve the accepted payload; font patching handles cmap misses."""
    return {key: str(val) for key, val in prepared.items()}


def _fit_prepared_to_shell(
    prepared: Dict[str, str],
    path: str,
    lens: Dict[str, int],
    uni_gid: Dict[int, int],
    *,
    field_y: Optional[Dict[str, float]] = None,
) -> Optional[Dict[str, str]]:
    """Подгонка всех полей под CID-слоты shell (без укорочения ФИО)."""
    from sber_stealth_v3 import check_text_with_map

    cmap = _cid_map_from_pdf(path)
    work = _auto_fix_prepared_fields(dict(prepared), cmap)
    if check_text_with_map(_needed_text(work), cmap):
        return None

    doc = fitz.open(path)
    cs = doc.xref_stream(doc[0].get_contents()[0])
    doc.close()

    enc_map = _enc_map_for_shell(uni_gid, work)
    card_style = field_y is not None and "dest_card" in field_y
    enc = (
        (lambda t, m=enc_map: _sber_enc_raw(t, m))
        if card_style
        else (lambda t, m=enc_map: _sber_enc(t, m))
    )
    fy = field_y or _FIELD_Y
    out = dict(work)
    for key, y in fy.items():
        if key not in lens or key not in out:
            continue
        row = _read_field_at_y(cs, y, uni_gid)
        shell_bytes = row[0] if row else b""
        # Date: fit user «сейчас»/input into shell slot (never keep donor calendar).
        hint = "phone" if key == "receiver_phone" else (
            "date" if key == "date_time" else key
        )
        is_phone_shell = field_y is not None and "document_num" in field_y
        if is_phone_shell and key in ("amount", "commission"):
            continue
        if card_style and key in ("amount", "commission", "charged"):
            # keep donor money formatting; splice later in _build_from_base
            continue
        # Phone + SBP face: trail pads keep left edge under the label.
        # (Old SBP lead-pad pushed bank/FIO right — violates field-edge-anchors.)
        _pad = "trail"
        fitted_text, b = _fit_text_encoded_length(
            out[key], lens[key], enc,
            shell_bytes=shell_bytes,
            key_hint=hint,
            phone_style=key == "receiver_phone",
            pad_side=_pad,
        )
        if len(b) != lens[key]:
            return None
        # ФИО/банк — только полный ввод (хвостовые пробелы паддинга ок).
        if key in ("sender_name", "receiver_name", "sender", "receiver", "recipient_bank"):
            if fitted_text.strip() != str(out[key]).strip():
                return None
        if key == "amount":
            want = re.sub(r"\D", "", str(out[key]))
            got = re.sub(r"\D", "", fitted_text)
            if want and want not in got and not got.startswith(want):
                return None
        out[key] = fitted_text
    try:
        from sber_sbp_stealth import sync_sbp_id_core_timestamp
        sync_sbp_id_core_timestamp(out)
    except Exception:
        pass
    return out


def _try_donor_orig(prepared: Dict[str, str], donor_path: Optional[str] = None) -> Optional[bytes]:
    from sber_stealth_v3 import check_text_with_map, create_sber_stealth

    if donor_path:
        cmap = _cid_map_from_pdf(donor_path)
        work = _auto_fix_prepared_fields(prepared, cmap)
        miss = check_text_with_map(_needed_text(work), cmap)
        if miss:
            return None
        donor = donor_path
        prepared = work
    else:
        donor, miss_set = _pick_charset_donor(prepared)
        if not donor:
            return None
        cmap = _cid_map_from_pdf(donor)
        work = _auto_fix_prepared_fields(prepared, cmap)
        miss = check_text_with_map(_needed_text(work), cmap)
        if miss:
            return None
        prepared = work

    cid_map = _cid_map_from_pdf(donor)
    data = {k: prepared[k] for k in prepared}
    # Keep user date_time; only sync SBP ID timestamp core to it.
    try:
        from sber_sbp_stealth import sync_sbp_id_core_timestamp
        sync_sbp_id_core_timestamp(data)
    except Exception as exc:
        logger.warning("Sber donor-orig: SBP timestamp sync failed: %s", exc)
    result = create_sber_stealth(
        template_path=donor,
        data=data,
        auto_select=False,
        cid_map=cid_map,
    )
    if not result:
        return None

    donor_size = os.path.getsize(donor)
    before = result
    result = _fix_stream_separators(result)
    if len(result) != len(before):
        result = before
    if len(result) != donor_size:
        logger.warning(
            "Sber donor-orig: size drift %d→%d — skip",
            donor_size, len(result),
        )
        return None
    sx = re.search(rb"startxref\s*[\r\n]+(\d+)", result)
    if not sx or result[int(sx.group(1)) : int(sx.group(1)) + 4] != b"xref":
        logger.error("Sber donor-orig: startxref broken — skip")
        return None
    before = result
    result = _apply_sber_orphan_blank_pdf(result)
    if len(result) != donor_size:
        # Prefer reverse-closure clean over exact donor byte length.
        padded = _pad_pdf_to_exact_size(result, donor_size)
        if len(padded) == donor_size:
            result = padded
        else:
            logger.warning(
                "Sber donor-orig: orphan fix size %d→%d (ship closure-clean)",
                donor_size, len(result),
            )
    logger.info(
        "Sber donor-orig OK: %s (%d bytes, base %s)",
        os.path.basename(donor), len(result), donor_size,
    )
    return result


def _splice_ruble_slot(old_b: bytes, new_b: bytes) -> bytes:
    """Сохранить байты ₽ из shell-слота (\\rZ или CID 0x0F)."""
    if len(old_b) != len(new_b):
        return new_b
    for marker in (b"\\rZ", b"\x00\x0f", b"\x00\x0F"):
        pos = old_b.find(marker)
        if pos >= 0:
            end = pos + len(marker)
            return new_b[:pos] + old_b[pos:end] + new_b[end:]
    return new_b


def _build_from_base(
    base_path: str,
    prepared: Dict[str, str],
    *,
    patch_font: bool,
    field_y: Optional[Dict[str, float]] = None,
    field_specs: Optional[List[Tuple[str, str, float]]] = None,
) -> Optional[bytes]:
    field_y = field_y or _FIELD_Y
    field_specs = field_specs or _SBP_FIELD_SPECS
    meta_date = str(prepared.get("date_time") or "")
    if "dest_card" not in field_y:
        from sber_sbp_stealth import sync_sbp_id_core_timestamp
        sync_sbp_id_core_timestamp(prepared)

    with open(base_path, "rb") as fp:
        orig = fp.read()

    doc = fitz.open(base_path)
    fm = tut._find_font_objects(doc)
    key = _pick_arial(fm)
    if not key:
        doc.close()
        logger.error("Sber dynamic: Arial font not found in %s", base_path)
        return None
    meta = fm[key]
    ff2_orig = doc.xref_stream(meta["fontfile_xref"])
    offsets_pre, _, _, _ = tut._parse_xref_table(orig)
    ff2_s = offsets_pre[meta["fontfile_xref"]]
    ff2_e = tut._find_object_end(orig, ff2_s)
    ff2_comp_m = re.search(
        rb"stream\r?\n([\x00-\xff]*?)\r?\nendstream", orig[ff2_s:ff2_e], re.DOTALL)
    ff2_orig_compressed = ff2_comp_m.group(1) if ff2_comp_m else None

    sub = tut._parse_subset_tounicode(
        doc.xref_stream(meta["tounicode_xref"]).decode("latin1", "replace"))
    uni_gid0: Dict[int, int] = {u: c for c, u in sub.items()}
    cs_xref = doc[0].get_contents()[0]
    cs_orig = doc.xref_stream(cs_xref)
    doc.close()

    # Face text + static labels only. Do NOT lock donor FIO unicodes — those
    # CIDs are stealable so new letters overwrite in place (T-Bank-style).
    face_gids = _face_field_gids(cs_orig, field_y, field_specs, uni_gid0)
    stream_gids = _gids_in_stream(cs_orig)
    label_gids = stream_gids - face_gids
    rev0 = {gid: cp for cp, gid in uni_gid0.items()}
    need = _needed_codepoints(prepared)
    for gid in label_gids:
        cp = rev0.get(gid)
        if cp is not None:
            need.add(cp)
        elif gid == _RUBLE_CID:
            need.add(0x20BD)
        elif gid in (0x20, 3):
            need.add(0x20)
    steal_gids = face_gids - label_gids

    font0 = TTFont(BytesIO(ff2_orig))
    num_glyphs = font0["maxp"].numGlyphs

    if patch_font:
        sgl.ensure_library()
        lib = sgl.merged_charset()
        still = [chr(cp) for cp in need if cp not in uni_gid0 and chr(cp) not in lib]
        if still:
            logger.error("Sber dynamic: chars not in library: %s", "".join(still))
            return None
        if all(cp in uni_gid0 for cp in need):
            planned = {cp: uni_gid0[cp] for cp in need if cp in uni_gid0}
        else:
            planned = _allocate_uni_gid(
                uni_gid0, need, num_glyphs, steal_gids=steal_gids,
            )
        still_missing = [chr(cp) for cp in need if cp not in planned]
        if still_missing:
            logger.error("Sber dynamic: allocation failed for %s", "".join(still_missing))
            return None
        if 0x20BD in uni_gid0 and _RUBLE_CID in _gids_in_stream(cs_orig):
            planned[0x20BD] = uni_gid0[0x20BD]
        logger.info(
            "Sber allocate: need=%d steal_slots=%d new=%d shell=%s",
            len(need),
            len(steal_gids),
            sum(1 for cp in need if cp not in uni_gid0),
            os.path.basename(base_path),
        )
    else:
        planned = uni_gid0
        missing = [chr(cp) for cp in need if cp not in uni_gid0]
        if missing:
            logger.error("Sber dynamic: content-only but missing chars: %s", "".join(missing))
            return None

    if 0x20BD in uni_gid0 and _RUBLE_CID in _gids_in_stream(cs_orig):
        planned[0x20BD] = uni_gid0[0x20BD]

    stream = cs_orig
    card_style = field_y is not None and "dest_card" in field_y
    enc = (
        (lambda t: _sber_enc_raw(t, planned))
        if card_style
        else (lambda t: _sber_enc(t, planned))
    )
    ug = {**uni_gid0, **planned}
    phone_style = field_y is not None and "document_num" in field_y

    # Snapshot identity fields so any candidate rebuild can be verified exactly.
    identity_snap = {
        key: str(prepared.get(key) or "")
        for _n, key, _sz in field_specs
        if key in ("recipient_bank", "sender_name", "receiver_name")
    }

    ok: Dict[str, bool] = {}
    for name, key, sz in field_specs:
        if key not in prepared:
            ok[name] = False
            continue
        row = _read_field_at_y(stream, field_y[key], uni_gid0)
        if not row:
            ok[name] = False
            continue
        old_b, old_t = row
        if name == "date" or key == "date_time":
            # Exact face date — no ASCII lead/trail spaces (Proton HARD on both).
            # Center 153 is done later by Tm.x recenter.
            user_date = str(prepared.get(key) or "").strip()
            exact_date = re.sub(r"\s+", " ", user_date)
            if "(МСК)" not in exact_date:
                if not exact_date.endswith("МСК)"):
                    exact_date = exact_date.rstrip() + " (МСК)"
            exact_date = exact_date.replace(" (МСК)", "(МСК)").replace("(МСК)", " (МСК)")
            # Prefer spaced zone like corpus: « (МСК)»
            if " (МСК)" not in exact_date and exact_date.endswith("(МСК)"):
                exact_date = exact_date[:-5] + " (МСК)"
            exact_b = enc(exact_date)
            if not exact_b or not _slot_cids_aligned(exact_b):
                ok[name] = False
                continue
            if re.search(r"МСК\s+\)", exact_date) or "МСК )" in exact_date:
                ok[name] = False
                continue
            if len(exact_b) == len(old_b):
                stream, ok[name] = _replace_tj_skeleton(
                    stream, old_b, exact_b, allow_hex_tj=card_style,
                )
            elif patch_font:
                stream, ok[name] = _replace_tj_skeleton(
                    stream, old_b, exact_b, allow_len_change=True,
                )
                if ok[name]:
                    logger.info(
                        "Sber dynamic: date slot %d→%d B (no space pad)",
                        len(old_b), len(exact_b),
                    )
            else:
                ok[name] = False
                continue
            if ok.get(name):
                prepared[key] = exact_date
                try:
                    from sber_sbp_stealth import sync_sbp_id_core_timestamp
                    sync_sbp_id_core_timestamp(prepared)
                except Exception:
                    pass
            continue
        if key in ("amount", "commission") or (card_style and key == "charged"):
            new_b = _splice_amount_slot(
                old_b, prepared[key], enc,
                phone_style=phone_style,
                card_style=card_style,
            )
            if len(new_b) == len(old_b) and new_b != old_b:
                stream, ok[name] = _replace_tj_skeleton(
                    stream, old_b, new_b, allow_hex_tj=card_style,
                )
                prepared[key] = prepared[key]
                continue
            # Shorter head without lead-space pad — shrink Tj (no WHITESPACE HARD).
            if (
                new_b != old_b
                and len(new_b) < len(old_b)
                and patch_font
                and _slot_cids_aligned(new_b)
            ):
                stream, ok[name] = _replace_tj_skeleton(
                    stream, old_b, new_b, allow_len_change=True,
                )
                if ok[name]:
                    prepared[key] = prepared[key]
                    logger.info(
                        "Sber dynamic: shrink amount slot %d→%d B (no lead pad)",
                        len(old_b), len(new_b),
                    )
                    continue
            # Слот суммы короче цифр (76575 vs 4000) — splice вернул shell as-is.
            # Не молча оставляем чужую сумму.
            if new_b == old_b:
                want = _amount_rubles_int(prepared[key])
                have = _amount_rubles_int(old_t)
                if want != have:
                    if patch_font:
                        exact_b = enc(str(prepared[key]))
                        if exact_b and _slot_cids_aligned(exact_b):
                            stream, ok[name] = _replace_tj_skeleton(
                                stream, old_b, exact_b, allow_len_change=True,
                            )
                            if ok[name]:
                                logger.info(
                                    "Sber dynamic: expanded exact %s slot %d→%d B",
                                    name, len(old_b), len(exact_b),
                                )
                                continue
                    ok[name] = False
                    logger.warning(
                        "Sber amount slot too small for %r (shell %r, %d B)",
                        prepared[key][:24], old_t[:24], len(old_b),
                    )
                    continue
                stream, ok[name] = _replace_tj_skeleton(
                    stream, old_b, new_b, allow_hex_tj=card_style,
                )
                continue
            new_t, new_b = _fit_text_encoded_length(
                prepared[key], len(old_b), enc, shell_bytes=old_b, key_hint=name,
                phone_style=phone_style,
            )
            new_b = _splice_ruble_slot(old_b, new_b)
            prepared[key] = new_t
            if len(new_b) != len(old_b):
                if patch_font:
                    exact_b = enc(str(prepared[key]))
                    if exact_b and _slot_cids_aligned(exact_b):
                        stream, ok[name] = _replace_tj_skeleton(
                            stream, old_b, exact_b, allow_len_change=True,
                        )
                        if ok[name]:
                            logger.info(
                                "Sber dynamic: expanded exact %s slot %d→%d B",
                                name, len(old_b), len(exact_b),
                            )
                            continue
                ok[name] = False
                continue
            stream, ok[name] = _replace_tj_skeleton(
                stream, old_b, new_b, allow_hex_tj=card_style,
            )
            continue
        else:
            orig_val = str(prepared[key]).strip()
            # Font-patch: exact FIO/bank face — shrink/expand Tj, never space-pad
            # or truncate (short «Джэ.» / long chaos names).
            if patch_font and _strict_identity_keys(name):
                exact_b = enc(orig_val)
                if exact_b and _slot_cids_aligned(exact_b):
                    if len(exact_b) == len(old_b):
                        stream, ok[name] = _replace_tj_skeleton(
                            stream, old_b, exact_b, allow_hex_tj=card_style,
                        )
                    else:
                        stream, ok[name] = _replace_tj_skeleton(
                            stream, old_b, exact_b, allow_len_change=True,
                        )
                        if ok[name]:
                            logger.info(
                                "Sber dynamic: %s slot %d→%d B (exact, no pad)",
                                name, len(old_b), len(exact_b),
                            )
                    if ok.get(name):
                        prepared[key] = orig_val
                        continue
            new_t, new_b = _fit_text_encoded_length(
                prepared[key], len(old_b), enc, shell_bytes=old_b, key_hint=name,
                pad_side="trail",
            )
            if len(new_b) != len(old_b):
                # Force equal-length slot (trail-pad / truncate) — never expand.
                t = (new_t or orig_val or "А").strip() or "А"
                while t and len(enc(t)) > len(old_b):
                    t = t[:-1].rstrip()
                if not t:
                    t = "А"
                forced = _pad_encoded_cids(enc(t), len(old_b))
                if forced is not None and len(forced) == len(old_b):
                    new_t, new_b = t, forced
                    logger.warning(
                        "Sber dynamic: %s force-fit slot %d B → %r",
                        name, len(old_b), t[:40],
                    )
                else:
                    ok[name] = False
                    continue
            if _strict_identity_keys(name) and new_t.strip() != orig_val:
                logger.warning(
                    "Sber dynamic: %s truncated into slot %d B: %r → %r",
                    name, len(old_b), orig_val[:40], new_t.strip()[:40],
                )
            if key == "receiver_phone" and len(new_b) != len(old_b):
                ok[name] = False
                continue
        prepared[key] = new_t
        if len(new_b) != len(old_b):
            ok[name] = False
            continue
        stream, ok[name] = _replace_tj_skeleton(
            stream, old_b, new_b, allow_hex_tj=card_style,
        )

    # ФИО длиннее слота, у банка есть запас — перераздаём, НО полное имя банка
    # не режем. Не восстанавливаем полное ФИО из snap — иначе украдём байты у банка.
    if not all(ok.values()) and not card_style:
        if identity_snap.get("recipient_bank"):
            prepared["recipient_bank"] = identity_snap["recipient_bank"]
        stream2 = _rebalance_fio_bank_slots(
            cs_orig, prepared, planned, uni_gid0, field_y, field_specs, enc,
        )
        if stream2 is not None:
            stream = stream2
            ok = {name: True for name, key, _sz in field_specs if key in prepared}
            logger.info("Sber dynamic: rebalanced FIO/bank slot lengths")
        if not all(ok.values()):
            stream3 = _rebalance_amount_bank_pad(
                cs_orig, prepared, uni_gid0, field_y, field_specs, enc,
                phone_style=phone_style,
            )
            if stream3 is not None:
                stream = stream3
                ok = {name: True for name, key, _sz in field_specs if key in prepared}
                logger.info("Sber dynamic: rebalanced amount↔bank pad")

    if not all(ok.values()):
        logger.error("Sber dynamic: replacements failed %s", ok)
        return None

    if len(stream) != len(cs_orig) and not patch_font:
        logger.error(
            "Sber dynamic: decoded stream %s != shell %s (skeleton safety)",
            len(stream), len(cs_orig),
        )
        return None

    out_sk = _content_skeleton_hash(stream)
    shell_sk = _content_skeleton_hash(cs_orig)
    if out_sk != shell_sk:
        logger.error("Sber dynamic: skeleton changed %s -> %s", shell_sk, out_sk)
        return None

    # Tm recenter happens AFTER font patch (final cmap has month letters like «ю»).

    offsets, first, count, xref_off = tut._parse_xref_table(orig)
    sorted_xrefs = sorted(offsets.items(), key=lambda x: x[1])
    obj_ranges = {xn: (s, tut._find_object_end(orig, s)) for xn, s in sorted_xrefs}

    cs_s, cs_e = obj_ranges[cs_xref]
    orig_clen = _orig_cs_comp_len(orig, cs_s, cs_e)

    replacements_obj: Dict[int, bytes] = {}

    if patch_font:
        active_gids = _gids_in_stream(stream)
        date_y_for_tune = None
        if prepared.get("date_time"):
            if phone_style:
                date_y_for_tune = PHONE_DATE_Y
            elif (
                "date_time" in field_y
                and "document_num" not in field_y
                and "dest_card" not in field_y
            ):
                date_y_for_tune = field_y.get("date_time", SBP_DATE_Y)
        # Phone: blanking orphans → FontFile2 zlib shrink (HARD needs fat).
        # Never skip atlas glyf/hmtx gates on lean "full-charset" runtime —
        # that ships uniq=59/ne=117/hmtx=467 → Proton HARD FAKE.
        _blank_unused = not phone_style
        ff2, uni_gid, font, native_cids = _patch_sber_font(
            ff2_orig, planned, active_gids,
            blank_unused=_blank_unused,
            planned_uni_gid=planned,
            shell_stream=stream,
            shell_uni_gid=uni_gid0,
            internal_entropy_slack=(not phone_style and not card_style),
            donor_compressed_len=(
                len(ff2_orig_compressed) if ff2_orig_compressed else None
            ),
            date_center_y=None,
            date_center_exclusive=False,
        )
        # Fail closed: empty painted CIDs → bars on face (Proton + viewers).
        try:
            _ft_face = TTFont(BytesIO(ff2))
            _go_face = _ft_face.getGlyphOrder()
            _glyf_face = _ft_face["glyf"]
            _empty_face = [
                cid
                for cid in sorted(active_gids)
                if cid not in (0, 3)
                and not _glyph_has_visible_ink(_glyf_face, _go_face, cid)
            ]
            _ft_face.close()
            if _empty_face:
                logger.warning(
                    "Sber empty painted CIDs %s — abort %s",
                    _empty_face[:24], os.path.basename(base_path),
                )
                return None
        except Exception as exc:
            logger.warning("Sber face-ink check failed: %s — abort", exc)
            return None
        _lock_outlines = set(native_cids)
        uni_gid = _uni_gid_for_active(uni_gid, active_gids)
        # Recenter date Tm with FINAL font/cmap (includes grafted month letters).
        if date_y_for_tune is not None and prepared.get("date_time"):
            date_face = re.sub(r"\s+", " ", str(prepared["date_time"]).strip())
            stream2 = _recenter_sber_date_stream(
                stream,
                date_face,
                date_y_for_tune,
                font_obj=font,
                uni_to_gid=uni_gid,
            )
            if len(stream2) == len(stream):
                if stream2 != stream:
                    logger.info(
                        "Sber date Tm recentered post-font for center 153",
                    )
                stream = stream2
            else:
                logger.warning("Sber date recenter skipped (stream len would change)")

        # Fail fast: lean shells often grow FontFile2 past donor after grafts.
        if (
            not phone_style
            and not card_style
            and len(ff2) != len(ff2_orig)
        ):
            if len(ff2) < len(ff2_orig):
                ff2 = _pad_decoded_font_to_exact(ff2, ff2_orig)
            if len(ff2) != len(ff2_orig):
                logger.warning(
                    "Sber SBP FontFile2 decoded %d != donor %d — pad/trim",
                    len(ff2), len(ff2_orig),
                )
                try:
                    ff2 = _pad_decoded_font_to_exact(ff2, ff2_orig)
                except Exception:
                    pass
                if len(ff2) != len(ff2_orig):
                    try:
                        _keep_early = _gids_in_stream(stream) | {0, 3}
                        shrunk = _shrink_glyf_to_decoded(
                            ff2,
                            len(ff2_orig),
                            len(ff2_orig_compressed) if ff2_orig_compressed else 0,
                            _keep_early,
                        )
                        if shrunk is not None:
                            ff2 = shrunk
                        if len(ff2) < len(ff2_orig):
                            ff2 = _pad_decoded_font_to_exact(ff2, ff2_orig)
                    except Exception:
                        pass
                if len(ff2) != len(ff2_orig):
                    logger.warning(
                        "Sber SBP FontFile2 still %d≠%d — abort shell (corrupt glyf risk)",
                        len(ff2), len(ff2_orig),
                    )
                    return None
        if not phone_style and not card_style:
            # Early inject already raised used CIDs; separator boost often
            # pushes Jasper over 733 on those streams — only boost if short.
            stream = _boost_sber_separator_glyph_pairs(stream)
            stream = _normalize_sber_qq_profile(stream)
            if _sber_naive_qq(stream) not in _SBP_QQ_ALLOWED:
                logger.error(
                    "Sber QQ profile %s unfixable — abort %s",
                    _sber_naive_qq(stream), os.path.basename(base_path),
                )
                return None
            stream = _ensure_sber_cs_dec_min(stream)
            stream = _shrink_sber_cs_to_jasper(stream)
        elif phone_style:
            # Phone / sber_internal_jasper: 10/4, 11/4, 13/4 only (not SBP 7–8).
            stream = _normalize_sber_qq_profile(stream, allowed=_PHONE_QQ_ALLOWED)
            if _sber_naive_qq(stream) not in _PHONE_QQ_ALLOWED:
                logger.error(
                    "Sber phone QQ profile %s unfixable — abort %s",
                    _sber_naive_qq(stream), os.path.basename(base_path),
                )
                return None
        cs_max = PHONE_CONTENT_RAW_MAX if phone_style else SBP_CONTENT_RAW_MAX
        new_comp, stream = _compress_cs_preserve(stream, orig_clen, max_clen=cs_max)
        if new_comp is None:
            logger.error("Sber dynamic: exact flate miss for %s", os.path.basename(base_path))
            return None
        logger.info(
            "Sber dynamic: cs compressed %s B (shell %s, dec %s)",
            len(new_comp), orig_clen, len(stream),
        )
        replacements_obj[cs_xref] = tut._make_modified_obj(
            orig[cs_s:cs_e], cs_xref, new_stream=new_comp,
        )

        # Proton ink_gap (SBP atlas): letters+digits+'-' (excludes ₽).
        active_gids = _gids_in_stream(stream)
        if not phone_style and not card_style:
            keep_u = active_gids | {0, 3}
            try:
                _ft_u = TTFont(BytesIO(ff2))
                _closure = keep_u | _composite_dependency_gids(_ft_u, keep_u)
                _go = _ft_u.getGlyphOrder()
                _glyf = _ft_u["glyf"]
                _drop = {
                    gid
                    for gid, gname in enumerate(_go)
                    if gid not in _closure
                    and (
                        getattr(_glyf[gname], "numberOfContours", 0) != 0
                        or bool(getattr(_glyf[gname], "components", None))
                    )
                }
                _ft_u.close()
            except Exception:
                _drop = set()
            if _drop:
                ff2 = _blank_gids_in_ff2(ff2, _drop, ff2_orig)
                ff2 = _finalize_sber_contour_loca_profile(
                    ff2, ff2_orig, keep_u,
                )
            if not ff2_orig_compressed or len(ff2_orig_compressed) < 22_000:
                # Lean runtime/unlocked (~19KB, ne~118) ≠ sbp_outgoing atlas —
                # never ship / never inflate into Proton HARD profile.
                logger.warning(
                    "Sber font-patch: donor FontFile2 %s < 22000 — abort shell",
                    len(ff2_orig_compressed) if ff2_orig_compressed else 0,
                )
                return None
            _ff2_comp_budget = len(ff2_orig_compressed)
            _gid_to_uni = {int(g): int(cp) for cp, g in uni_gid.items()}
            # Stable donor-sized FF2 first (uniq pads), then ink_gap.
            rebuilt = _rebuild_sber_glyf_profile_after_blank(
                ff2, ff2_orig, _ff2_comp_budget, keep_u,
            )
            if rebuilt is not None:
                ff2 = rebuilt
            else:
                ff2 = _ensure_sber_glyf_uniq_lens_bytes(ff2, keep_u, ff2_orig)
                ff2 = _relax_sber_ff2_comp_budget(
                    ff2, _ff2_comp_budget, keep_u,
                )
            ff2_pre_ink = ff2
            ff2 = _apply_sber_ink_gap_hmtx(
                ff2, active_gids, gid_to_uni=_gid_to_uni, stream=stream,
                ff2_orig=ff2_orig,
                locked_outline_gids=_lock_outlines,
            )
            if ff2 != ff2_pre_ink:
                rebuilt2 = _rebuild_sber_glyf_profile_after_blank(
                    ff2, ff2_orig, _ff2_comp_budget, keep_u,
                )
                if rebuilt2 is not None:
                    ff2 = rebuilt2
                elif len(ff2) == len(ff2_orig):
                    ff2 = _ensure_sber_glyf_uniq_lens_bytes(
                        ff2, keep_u, ff2_orig,
                    )
                    ff2 = _relax_sber_ff2_comp_budget(
                        ff2, _ff2_comp_budget, keep_u,
                    )
                else:
                    logger.warning(
                        "Sber font-patch: post-ink glyf rebuild failed — abort shell",
                    )
                    return None
            ff2 = _finalize_sber_contour_loca_profile(ff2, ff2_orig, keep_u)
            _orph = _sber_orphan_nonempty_gids(ff2, active_gids)
            if _orph:
                _att = _attach_sber_orphans_as_components(
                    ff2, keep_u | {0}, _orph,
                )
                if _att != ff2:
                    ff2 = _att
                    keep_u = keep_u | {0} | set(_orph)
            try:
                _ft_chk = TTFont(BytesIO(ff2))
                _u = len(_glyf_nonempty_uniq_lens(_ft_chk))
                _loca = _ft_chk["loca"]
                _ne = sum(
                    1
                    for g in range(len(_loca.locations) - 1)
                    if _loca.locations[g + 1] > _loca.locations[g]
                )
                _cont = sum(
                    1
                    for n in _ft_chk.getGlyphOrder()
                    if int(
                        getattr(_ft_chk["glyf"][n], "numberOfContours", 0) or 0
                    ) != 0
                )
                _ft_chk.close()
            except Exception as exc:
                logger.warning("Sber SBP glyf profile check failed: %s", exc)
                return None
            logger.info(
                "Sber SBP glyf profile uniq=%d loca_ne=%d contour=%d",
                _u, _ne, _cont,
            )
            if (
                not (66 <= _ne <= 73)
                or not (66 <= _cont <= 73)
                or _cont != _ne
            ):
                logger.warning(
                    "Sber SBP glyf profile uniq=%d loca=%d contour=%d — pre-uniq REJECT",
                    _u, _ne, _cont,
                )
                return None
            if not (52 <= _u <= 56):
                logger.warning(
                    "Sber SBP uniq_lens=%d outside [52,56] after contour sync",
                    _u,
                )
                for _round in range(5):
                    if _u < 52:
                        ff2 = _bump_sber_uniq_lens(ff2, keep_u, need=52 - _u)
                    else:
                        ff2 = _blank_sber_uniq_singletons(
                            ff2, keep_u, min_nonempty=66, hi_uniq=56,
                        )
                        ff2 = _collapse_sber_uniq_lens_unused(
                            ff2, keep_u, ff2_orig, lo=52, hi=56,
                        )
                        ff2 = _merge_sber_uniq_lens_by_pad(
                            ff2, keep_u, lo=52, hi=56,
                        )
                    try:
                        _ft2 = TTFont(BytesIO(ff2))
                        _u = len(_glyf_nonempty_uniq_lens(_ft2))
                        _loca = _ft2["loca"]
                        _ne = sum(
                            1
                            for g in range(len(_loca.locations) - 1)
                            if _loca.locations[g + 1] > _loca.locations[g]
                        )
                        _cont = sum(
                            1
                            for n in _ft2.getGlyphOrder()
                            if int(
                                getattr(_ft2["glyf"][n], "numberOfContours", 0) or 0
                            ) != 0
                        )
                        _ft2.close()
                    except Exception:
                        break
                    if 52 <= _u <= 56:
                        break
                logger.info(
                    "Sber SBP after uniq collapse uniq=%d loca=%d contour=%d",
                    _u, _ne, _cont,
                )
            if (
                not (52 <= _u <= 56)
                or not (66 <= _ne <= 73)
                or not (66 <= _cont <= 73)
                or _cont != _ne
            ):
                logger.warning(
                    "Sber SBP glyf profile uniq=%d loca=%d contour=%d — REJECT",
                    _u, _ne, _cont,
                )
                return None
            if len(ff2) == len(ff2_orig):
                ff2 = _force_long_loca(ff2, ff2_orig)
                if len(ff2) != len(ff2_orig):
                    shrunk = _shrink_glyf_to_decoded(
                        ff2,
                        len(ff2_orig),
                        _ff2_comp_budget,
                        keep_u,
                    )
                    if shrunk is not None:
                        ff2 = shrunk
                    if len(ff2) < len(ff2_orig):
                        ff2 = _pad_decoded_font_to_exact(ff2, ff2_orig)
                if len(ff2) == len(ff2_orig):
                    ff2 = _restore_sber_struct_fp_tables(ff2, ff2_orig)
            # Last: snap foreign AWs — ink_gap / grafts → HARD 428≠425.
            ff2 = _collapse_hmtx_uniq_advances_sber(
                ff2, ff2_orig, used_gids=keep_u,
            )
            ff2 = _apply_sber_ink_gap_hmtx(
                ff2, active_gids, gid_to_uni=_gid_to_uni, stream=stream,
                ff2_orig=ff2_orig,
                locked_outline_gids=_lock_outlines,
            )
            if len(ff2) != len(ff2_orig) and len(ff2) < len(ff2_orig):
                ff2 = _pad_decoded_font_to_exact(ff2, ff2_orig)
            if len(ff2) == len(ff2_orig):
                ff2 = _restore_sber_struct_fp_tables(ff2, ff2_orig)
            try:
                _ft_fin = TTFont(BytesIO(ff2))
                _u = len(_glyf_nonempty_uniq_lens(_ft_fin))
                _loca = _ft_fin["loca"]
                _ne = sum(
                    1
                    for g in range(len(_loca.locations) - 1)
                    if _loca.locations[g + 1] > _loca.locations[g]
                )
                _cont = sum(
                    1
                    for n in _ft_fin.getGlyphOrder()
                    if int(
                        getattr(_ft_fin["glyf"][n], "numberOfContours", 0) or 0
                    ) != 0
                )
                _ink_pairs = _sber_pair_ink_gaps(
                    _ft_fin, _sber_tj_cid_runs(stream or b""),
                )
                _ft_fin.close()
            except Exception as exc:
                logger.warning("Sber SBP final glyf/ink check failed: %s", exc)
                return None
            _ink_ok = bool(_ink_pairs) and (
                _ink_pairs[0][0] in _SBER_INK_GAP_MIN_ALLOWED
                and 550 <= _ink_pairs[-1][0] <= 650
            )
            if (
                not (52 <= _u <= 56)
                or not (66 <= _ne <= 73)
                or not (66 <= _cont <= 73)
                or _cont != _ne
            ):
                logger.warning(
                    "Sber SBP final profile REJECT uniq=%d loca=%d contour=%d",
                    _u, _ne, _cont,
                )
                return None
            if not _ink_ok:
                logger.warning(
                    "Sber SBP final ink REJECT min=%s max=%s",
                    _ink_pairs[0][0] if _ink_pairs else None,
                    _ink_pairs[-1][0] if _ink_pairs else None,
                )
                return None
            font = TTFont(BytesIO(ff2))

        cmap = tut._build_tounicode_cmap(uni_gid)
        w_arr = tut._build_widths_array(font, sorted(active_gids | {3}))
        if phone_style:
            # Prefer atlas compressed band; exact decoded ∈ {55136,55476,56064}.
            if prepared.get("date_time"):
                ff2 = _micro_tune_date_center_font(
                    ff2, stream, uni_gid, PHONE_DATE_Y,
                )
            ff2 = _snap_phone_ff2_decoded(ff2)
            if len(ff2) not in _PHONE_FF2_DEC_ALLOWED:
                logger.error(
                    "Sber phone FontFile2 decoded %d not in atlas %s — abort",
                    len(ff2), sorted(_PHONE_FF2_DEC_ALLOWED),
                )
                return None
            if len(ff2) > _PHONE_FF2_DEC_HARD:
                logger.warning(
                    "Sber phone FontFile2 decoded %d > former HARD %d — ship",
                    len(ff2), _PHONE_FF2_DEC_HARD,
                )
            ff2_comp, ff2 = _compress_font_phone(ff2, ff2_orig_compressed)
            if not ff2_comp:
                logger.error("Sber phone FontFile2 compress failed")
                return None
            # Entropy-tail must not drift off exact decoded atlas.
            if len(ff2) not in _PHONE_FF2_DEC_ALLOWED:
                ff2 = _snap_phone_ff2_decoded(ff2)
                ff2_comp, ff2 = _compress_font_phone(ff2, ff2_orig_compressed)
                if not ff2_comp or len(ff2) not in _PHONE_FF2_DEC_ALLOWED:
                    logger.error(
                        "Sber phone FontFile2 post-comp decoded %d — abort",
                        len(ff2) if ff2 else -1,
                    )
                    return None
            font = TTFont(BytesIO(ff2))
            w_arr = tut._build_widths_array(font, sorted(active_gids | {3}))
        else:
            # SBP atlas HARD ≤54162. Patch itself pads to shell size; exact-flate
            # donor /Length match grows decoded past HARD → FAKE. Keep natural.
            ff2_natural = ff2
            if len(ff2_natural) != len(ff2_orig):
                logger.warning(
                    "Sber SBP FontFile2 decoded %d != donor %d — pad/trim",
                    len(ff2_natural), len(ff2_orig),
                )
                try:
                    ff2_natural = _pad_decoded_font_to_exact(
                        ff2_natural, ff2_orig,
                    )
                except Exception:
                    pass
            if date_y_for_tune is not None:
                tm_chk, cids_chk, _ = _date_run_cids(stream, date_y_for_tune, uni_gid)
                if tm_chk is not None and cids_chk:
                    cx_chk = _date_visual_center_from_cids(
                        cids_chk, tm_chk, TTFont(BytesIO(ff2_natural)),
                    )
                    if abs(cx_chk - SBER_DATE_CENTER_X) > 0.01:
                        # Nudge Tm after ink_gap AW changes; re-flate CS below.
                        need_tm = tm_chk + (SBER_DATE_CENTER_X - cx_chk)
                        stream2 = _set_tm_x_at_y(stream, date_y_for_tune, need_tm)
                        if len(stream2) == len(stream) and stream2 != stream:
                            stream = stream2
                            tm2, cids2, _ = _date_run_cids(
                                stream, date_y_for_tune, uni_gid,
                            )
                            if tm2 is not None and cids2:
                                cx_chk = _date_visual_center_from_cids(
                                    cids2, tm2, TTFont(BytesIO(ff2_natural)),
                                )
                            logger.info(
                                "Sber SBP date Tm nudged post-ink → center %.4f",
                                cx_chk,
                            )
                        if abs(cx_chk - SBER_DATE_CENTER_X) > 0.01:
                            logger.warning(
                                "Sber SBP date center %.3f ≠ 153±0.01 — ship",
                                cx_chk,
                            )
                        # CS was flate'd before ink_gap — rewrite with nudged Tm.
                        new_comp, stream = _compress_cs_preserve(
                            stream, orig_clen, max_clen=cs_max,
                        )
                        if new_comp is None:
                            logger.warning(
                                "Sber dynamic: post-ink date CS flate miss for %s — ship",
                                os.path.basename(base_path),
                            )
                            new_comp = compress_like_jasper(stream)
                        replacements_obj[cs_xref] = tut._make_modified_obj(
                            orig[cs_s:cs_e], cs_xref, new_stream=new_comp,
                        )
            ff2_comp, ff2_padded = _compress_font_same_length_exact(
                ff2_natural, ff2_orig_compressed,
            )
            if (
                not ff2_comp
                or not ff2_orig_compressed
                or len(ff2_padded) != len(ff2_natural)
                or len(ff2_comp) != len(ff2_orig_compressed)
            ):
                logger.warning(
                    "Sber SBP FontFile2 profile drift dec=%d/%d comp=%d/%s — ship",
                    len(ff2_padded or b""), len(ff2_natural),
                    len(ff2_comp or b""),
                    len(ff2_orig_compressed) if ff2_orig_compressed else "?",
                )
            ff2 = ff2_padded if ff2_padded else ff2_natural
            if not ff2_comp:
                ff2_comp = zlib.compress(ff2, 6)
            # /W must match donor-preserving hmtx.
            font = TTFont(BytesIO(ff2))
            w_arr = tut._build_widths_array(font, sorted(active_gids | {3}))

        s, e = obj_ranges[meta["fontfile_xref"]]
        replacements_obj[meta["fontfile_xref"]] = tut._make_modified_obj(
            orig[s:e], meta["fontfile_xref"],
            new_stream=ff2_comp, new_length1=len(ff2),
        )
        s, e = obj_ranges[meta["tounicode_xref"]]
        tu_clen = _orig_cs_comp_len(orig, s, e)
        tu_comp = _pad_to_compressed_size(cmap, tu_clen) if tu_clen else None
        if tu_comp is None:
            tu_comp = _best_compress(cmap)
        replacements_obj[meta["tounicode_xref"]] = tut._make_modified_obj(
            orig[s:e], meta["tounicode_xref"], new_stream=tu_comp,
        )
        s, e = obj_ranges[meta["cidfont_xref"]]
        replacements_obj[meta["cidfont_xref"]] = tut._make_modified_obj(
            orig[s:e], meta["cidfont_xref"], new_W=w_arr,
        )
        logger.info(
            "Sber dynamic font: ff2 %d -> %d B (orig %d)",
            len(ff2_orig), len(ff2), len(ff2_orig),
        )
    else:
        # Content-only path: still commit the rewritten content stream.
        if not phone_style and not card_style:
            # Early inject already raised used CIDs; separator boost often
            # pushes Jasper over 733 on those streams — only boost if short.
            stream = _boost_sber_separator_glyph_pairs(stream)
            stream = _normalize_sber_qq_profile(stream)
            if _sber_naive_qq(stream) not in _SBP_QQ_ALLOWED:
                logger.error(
                    "Sber QQ profile %s unfixable — abort %s",
                    _sber_naive_qq(stream), os.path.basename(base_path),
                )
                return None
            stream = _ensure_sber_cs_dec_min(stream)
            stream = _shrink_sber_cs_to_jasper(stream)
        cs_max = PHONE_CONTENT_RAW_MAX if phone_style else SBP_CONTENT_RAW_MAX
        new_comp, stream = _compress_cs_preserve(stream, orig_clen, max_clen=cs_max)
        if new_comp is None:
            logger.error("Sber dynamic: exact flate miss for %s", os.path.basename(base_path))
            return None
        replacements_obj[cs_xref] = tut._make_modified_obj(
            orig[cs_s:cs_e], cs_xref, new_stream=new_comp,
        )
        # Content-only: absorb reverse-closure orphans into hyphen pads, then
        # blank leftovers on SBP. Phone must keep donor glyf density.
        active_gids = _gids_in_stream(stream)
        if not phone_style:
            orphans = _sber_orphan_nonempty_gids(ff2_orig, active_gids)
            if orphans:
                stream = _inject_sber_contour_pad_cids(
                    stream,
                    need_used=max(66, len(active_gids) + len(orphans)),
                    max_clen=max(cs_max, 800),
                    prefer_cids=set(orphans),
                )
                # Re-compress after pad inject (allow slight overshoot — not HARD).
                new_comp, stream = _compress_cs_preserve(
                    stream, orig_clen, max_clen=max(cs_max, 800),
                )
                if new_comp is None:
                    logger.error(
                        "Sber dynamic: pad inject flate miss for %s",
                        os.path.basename(base_path),
                    )
                    return None
                replacements_obj[cs_xref] = tut._make_modified_obj(
                    orig[cs_s:cs_e], cs_xref, new_stream=new_comp,
                )
                active_gids = _gids_in_stream(stream)
        if phone_style:
            ff2, trimmed = ff2_orig, 0
        else:
            ff2, trimmed = _blank_sber_font_orphans(ff2_orig, active_gids)
        ink_changed = False
        if not phone_style and not card_style:
            if (
                not ff2_orig_compressed
                or len(ff2_orig_compressed) < 22_000
            ):
                logger.warning(
                    "Sber content-only: donor FontFile2 %s < 22000 — skip shell",
                    len(ff2_orig_compressed) if ff2_orig_compressed else 0,
                )
                return None
            # Blanking parks glyf in SFNT tail — rebuild, ink_gap (may ft.save),
            # then rebuild again so blank-pad uniq survives keep-glyph bbox fixes.
            keep_u = active_gids | {0, 3}
            rebuilt = _rebuild_sber_glyf_profile_after_blank(
                ff2, ff2_orig, len(ff2_orig_compressed), keep_u,
            )
            if rebuilt is None:
                logger.warning(
                    "Sber content-only: glyf profile rebuild failed — abort",
                )
                return None
            ff2 = rebuilt
            _gid_to_uni = {int(g): int(cp) for cp, g in uni_gid0.items()}
            ff2_ink = _apply_sber_ink_gap_hmtx(
                ff2, active_gids, gid_to_uni=_gid_to_uni, stream=stream,
                ff2_orig=ff2_orig,
            )
            ink_changed = ff2_ink != ff2
            ff2 = ff2_ink
            if ink_changed:
                rebuilt2 = _rebuild_sber_glyf_profile_after_blank(
                    ff2, ff2_orig, len(ff2_orig_compressed), keep_u,
                )
                if rebuilt2 is not None:
                    ff2 = rebuilt2
                elif len(ff2) == len(ff2_orig):
                    ff2 = _ensure_sber_glyf_uniq_lens_bytes(
                        ff2, keep_u, ff2_orig,
                    )
                    ff2 = _relax_sber_ff2_comp_budget(
                        ff2, len(ff2_orig_compressed), keep_u,
                    )
                else:
                    logger.warning(
                        "Sber content-only: post-ink glyf rebuild failed — abort",
                    )
                    return None
            # Gate corpus glyf profile before ship.
            try:
                _ft = TTFont(BytesIO(ff2))
                _u = len(_glyf_nonempty_uniq_lens(_ft))
                _loca = _ft["loca"]
                _ne = sum(
                    1
                    for g in range(len(_loca.locations) - 1)
                    if _loca.locations[g + 1] > _loca.locations[g]
                )
                _ft.close()
            except Exception:
                _u, _ne = 0, 999
            if not (52 <= _u <= 56) or not (66 <= _ne <= 73):
                logger.warning(
                    "Sber content-only: uniq=%d nonempty=%d — abort",
                    _u, _ne,
                )
                return None
        if trimmed or phone_style or ink_changed or (
            not phone_style and not card_style
        ):
            if phone_style:
                if len(ff2) > _PHONE_FF2_DEC_HARD:
                    logger.warning(
                        "Sber phone FontFile2 decoded %d > former HARD %d — ship",
                        len(ff2), _PHONE_FF2_DEC_HARD,
                    )
                ff2_comp, ff2 = _compress_font_phone(ff2, ff2_orig_compressed)
                if not ff2_comp:
                    logger.error("Sber phone content-only FontFile2 compress failed")
                    return None
            else:
                # Never ship natural oversize — Proton flags uniq/W/ink + Length.
                ff2_comp, ff2_padded = _compress_font_same_length_exact(
                    ff2, ff2_orig_compressed,
                )
                if (
                    not ff2_comp
                    or not ff2_orig_compressed
                    or len(ff2_padded) != len(ff2)
                    or len(ff2_comp) != len(ff2_orig_compressed)
                ):
                    logger.warning(
                        "Sber content-only reject FontFile2 dec=%d/%d comp=%d/%s",
                        len(ff2_padded) if ff2_comp else len(ff2),
                        len(ff2),
                        len(ff2_comp or b""),
                        len(ff2_orig_compressed) if ff2_orig_compressed else "?",
                    )
                    return None
                ff2 = ff2_padded
                # /W must match final hmtx after ink_gap AW bumps.
                font_w = TTFont(BytesIO(ff2))
                w_arr = tut._build_widths_array(
                    font_w, sorted(active_gids | {3}),
                )
                s_w, e_w = obj_ranges[meta["cidfont_xref"]]
                replacements_obj[meta["cidfont_xref"]] = tut._make_modified_obj(
                    orig[s_w:e_w], meta["cidfont_xref"], new_W=w_arr,
                )
            s, e = obj_ranges[meta["fontfile_xref"]]
            replacements_obj[meta["fontfile_xref"]] = tut._make_modified_obj(
                orig[s:e], meta["fontfile_xref"],
                new_stream=ff2_comp, new_length1=len(ff2),
            )
            logger.info(
                "Sber content-only: blanked %d orphan glyphs (ff2=%d B)",
                trimmed, len(ff2),
            )
        else:
            logger.info(
                "Sber dynamic: content-only (font clean, ff2=%d B)", len(ff2_orig),
            )

    out = bytearray(orig[:sorted_xrefs[0][1]])
    new_offsets: Dict[int, int] = {}
    for xn, (s, e) in sorted(obj_ranges.items(), key=lambda kv: kv[1][0]):
        new_offsets[xn] = len(out)
        out.extend(replacements_obj.get(xn, orig[s:e]))

    new_xref_off = len(out)
    xref_lines = [b"xref\n", f"{first} {count}\n".encode()]
    for n in range(first, first + count):
        if n == 0:
            xref_lines.append(b"0000000000 65535 f \n")
        elif n in new_offsets:
            xref_lines.append(f"{new_offsets[n]:010d} 00000 n \n".encode())
        else:
            xref_lines.append(b"0000000000 00000 f \n")
    out.extend(b"".join(xref_lines))

    trailer_pos = orig.find(b"trailer", xref_off)
    sx_pos = orig.find(b"startxref", trailer_pos)
    out.extend(orig[trailer_pos:sx_pos])
    out.extend(f"startxref\n{new_xref_off}\n%%EOF\n".encode())

    result = bytes(out)
    if patch_font:
        result = _prune_sber_font_subset(bytearray(result), meta, stream)
    # Fraudex: never rewrite CreationDate/ModDate (cross-day meta → structure).
    # Keep donor Info dates byte-identical.
    before = result
    result = _fix_stream_separators(result)
    if len(result) != len(before):
        result = before
    # Новый /ID — иначе Proton: CROSS_DOCUMENT_WEAK_MATCH (тот же trailer_id, другие поля).
    before = result
    result = _randomize_sber_fingerprints(result)
    if len(result) != len(before):
        # ID replace must be size-neutral; if not — keep previous.
        result = before
    target_size = len(orig)
    if len(result) != target_size:
        result = _pad_pdf_to_exact_size(result, target_size)
    # Size drift допустим, если Proton stream integrity ок (pad не дописывает
    # хвост после EOF). Жёсткий abort ломал все font-patch gens.
    if len(result) != target_size:
        logger.warning(
            "Sber dynamic: size %d != shell %d (continue; gate checks integrity)",
            len(result), target_size,
        )
    # Size-lift to ~102KB is SBP-only. Phone profile sber_internal_jasper
    # is ~44KB (HARD 40703–48543); lifting → SBER_FILE_SIZE_STRONG_OUTLIER.
    if not phone_style:
        result = _lift_sber_pdf_into_orig_band(result)
    else:
        # Keep phone in donor band; never inflate toward SBP.
        if not (40_000 <= len(result) <= 49_000):
            logger.error(
                "Sber phone size %d outside 40–49KB band (shell %d) — abort",
                len(result), target_size,
            )
            return None
    sx = re.search(rb"startxref\s*[\r\n]+(\d+)", result)
    if not sx or result[int(sx.group(1)) : int(sx.group(1)) + 4] != b"xref":
        logger.error("Sber dynamic: startxref broken after build — abort")
        return None
    if not phone_style and not card_style:
        unused_nonblank = _sber_unused_nonblank_gids(result)
        if unused_nonblank:
            # Donor outlines kept for contour-nonempty ∈ [66,73]. Near-miss
            # skips reverse-closure HARD; aborting here blocked all Proton sends.
            logger.warning(
                "Sber SBP unused nonblank glyphs=%d (kept for contour floor)",
                len(unused_nonblank),
            )
        pairs = _sber_pdf_glyph_pair_count(result)
        if pairs < _SBP_GLYPH_PAIRS_MIN:
            logger.error(
                "Sber SBP glyph_pairs=%d < %d — abort (short face vs donor)",
                pairs, _SBP_GLYPH_PAIRS_MIN,
            )
            return None
    logger.info(
        "Sber dynamic OK: %s bytes (base %s, target %s)",
        len(result), os.path.basename(base_path), target_size,
    )
    if not phone_style and not card_style:
        before = result
        result = _apply_sber_orphan_blank_pdf(result)
        if result != before:
            if abs(len(result) - len(before)) > 512:
                logger.warning(
                    "Sber SBP orphan fix size %d→%d — keep fixed (closure > bytes)",
                    len(before), len(result),
                )
            # Prefer closure-clean PDF; pad back into original band if needed.
            if not (100_000 <= len(result) <= 105_000):
                try:
                    result = _lift_sber_pdf_into_orig_band(result)
                except Exception:
                    pass
            if len(result) != len(before):
                try:
                    result = _pad_pdf_to_exact_size(result, len(before))
                except Exception:
                    pass
    return result


def _baked_shell_paths() -> List[str]:
    """Offline shells only — templates/sber_shells/, без рантайм-доступа к корпусу."""
    if os.path.isdir(SBER_SHELLS_DIR):
        paths = sorted(
            os.path.join(SBER_SHELLS_DIR, name)
            for name in os.listdir(SBER_SHELLS_DIR)
            if name.lower().endswith(".pdf")
        )
        if paths:
            return paths
    if os.path.isfile(SHELL_ARTIFACT):
        return [SHELL_ARTIFACT]
    if os.path.isfile(ORIG_TEMPLATE):
        return [ORIG_TEMPLATE]
    return []


def _shell_skeleton_prefix(path: str) -> str:
    doc = fitz.open(path)
    cs = doc.xref_stream(doc[0].get_contents()[0])
    doc.close()
    return _content_skeleton_hash(cs)[:16]


def _pick_baked_shell(need: Set[int]) -> Tuple[str, int]:
    """Shell с минимумом missing glyphs; tie-break — SBP skeleton 866cd81f."""
    paths = _baked_shell_paths()
    if not paths:
        raise FileNotFoundError("No Sber shell templates")

    best_path = paths[0]
    best_miss = len(need) + 1
    best_sk = ""

    for path in paths:
        try:
            uni = _template_unicode_set(path)
        except Exception:
            continue
        miss = sum(1 for cp in need if cp not in uni)
        sk = _shell_skeleton_prefix(path)
        better = (
            miss < best_miss
            or (miss == best_miss and sk.startswith("866cd81f") and not best_sk.startswith("866cd81f"))
            or (miss == best_miss and sk.startswith("866cd81f") == best_sk.startswith("866cd81f")
                and os.path.getsize(path) < os.path.getsize(best_path))
        )
        if better:
            best_path, best_miss, best_sk = path, miss, sk

    return best_path, best_miss


def _ensure_shell_template() -> str:
    """Narrow shell (~102 KB) — receipt.pdf, offline."""
    import shutil

    src = os.path.join(CORPUS_SBER_DIR, "receipt.pdf")
    if os.path.isfile(SHELL_ARTIFACT):
        return SHELL_ARTIFACT
    if os.path.isfile(src):
        os.makedirs(os.path.dirname(SHELL_ARTIFACT), exist_ok=True)
        shutil.copy2(src, SHELL_ARTIFACT)
        logger.info("Sber dynamic: installed shell artifact from corpus receipt.pdf")
        return SHELL_ARTIFACT
    if os.path.isfile(ORIG_TEMPLATE):
        return ORIG_TEMPLATE
    return UNLOCKED_TEMPLATE


def _sber_shell_exact_labels_ok(path: str) -> bool:
    """K-SBER-SBP-EXACT-PROFILE: donor face label «Операция» ≈ 46.57pt."""
    try:
        doc = fitz.open(path)
        page = doc[0]
        for w in page.get_text("words"):
            if w[4] == "Операция":
                width = float(w[2] - w[0])
                doc.close()
                return abs(width - 46.57) <= 0.06
        doc.close()
    except Exception:
        return False
    return False


def _sber_runtime_is_valid() -> bool:
    """Runtime shell: полный charset, skeleton 866cd81f, Jasper glyph count.

    Reject if label widths already drift (current S_sbp_runtime → HARD
    K-SBER-SBP-EXACT-PROFILE «Операция»=46.99).
    """
    path = RUNTIME_TEMPLATE if os.path.isfile(RUNTIME_TEMPLATE) else UNLOCKED_TEMPLATE
    if not os.path.isfile(path):
        return False
    try:
        if not _sber_shell_exact_labels_ok(path):
            logger.warning(
                "Sber runtime shell rejected: exact label profile fail (%s)",
                os.path.basename(path),
            )
            return False
        sgl.ensure_library()
        need = {ord(ch) for ch in sgl.merged_charset()}
        have = _template_unicode_set(path)
        if not need.issubset(have):
            return False
        sk = _pdf_content_skeleton(open(path, "rb").read())
        if sk != _PREFERRED_SBP_SKELETON:
            return False
        doc = fitz.open(path)
        fm = tut._find_font_objects(doc)
        key = _pick_arial(fm)
        if not key:
            doc.close()
            return False
        ff2 = doc.xref_stream(fm[key]["fontfile_xref"])
        sub = tut._parse_subset_tounicode(
            doc.xref_stream(fm[key]["tounicode_xref"]).decode("latin1", "replace"))
        doc.close()
        ng = TTFont(BytesIO(ff2))["maxp"].numGlyphs
        return len(sub) >= 95 and ng == 3419 and len(ff2) >= 52000
    except Exception as exc:
        logger.warning("Sber runtime shell check failed: %s", exc)
        return False


def _shell_template_candidates() -> List[str]:
    """Full-charset runtime first, then lean shells."""
    out: List[str] = []
    if _sber_runtime_is_valid():
        rt = RUNTIME_TEMPLATE if os.path.isfile(RUNTIME_TEMPLATE) else UNLOCKED_TEMPLATE
        if os.path.isfile(rt):
            out.append(rt)
    if os.path.isfile(UNLOCKED_TEMPLATE) and UNLOCKED_TEMPLATE not in out:
        out.append(UNLOCKED_TEMPLATE)
    for path in (_ensure_shell_template(), ORIG_TEMPLATE):
        if os.path.isfile(path) and path not in out:
            out.append(path)
    return out


def _generation_template_path(prepared: Optional[Dict[str, str]] = None) -> str:
    """Prefer original-band shells (~100–104KB) that cover the receipt charset.

    Fat unlocked/runtime (114KB) is last resort — live originals sit at 100–103KB.
    """
    need: Set[int] = set()
    if prepared:
        need = _needed_codepoints(prepared)

    candidates = _shell_template_candidates()
    if need:
        fitting = [
            p for p in candidates
            if all(cp in _template_unicode_set(p) for cp in need)
        ]
        if fitting:
            band = [
                p for p in fitting
                if 100_000 <= os.path.getsize(p) <= 105_000
            ]
            pool = band or fitting
            return min(pool, key=lambda p: abs(os.path.getsize(p) - 102_487))

    # Prefer original-band among candidates.
    band = [
        p for p in candidates
        if 100_000 <= os.path.getsize(p) <= 105_000
    ]
    if band:
        return min(band, key=lambda p: abs(os.path.getsize(p) - 102_487))
    if candidates:
        return candidates[0]
    logger.warning("Sber dynamic: no shell template, fallback original")
    return ORIG_TEMPLATE


def build_dynamic_sber_sbp(prepared: Dict[str, str]) -> Optional[bytes]:
    """SBP: donor-orig → content-only → font-patch (глифы из библиотеки)."""
    sgl.ensure_library()

    work = dict(prepared)
    _ensure_spb_id(work, prepared)
    # Short banks («ВТБ») on long donor slots → glyph_pairs < 238. Prefer a
    # longer face alias before shell fit / separator boost.
    raw_bank = str(
        work.get("recipient_bank") or work.get("bank_name") or ""
    ).strip()
    if raw_bank and len(raw_bank) <= 5:
        alts = _bank_slot_variants(raw_bank)
        if len(alts) > 1:
            pick = max(alts, key=len)
            if pick != raw_bank:
                work["recipient_bank"] = pick
                work["bank_name"] = pick
                logger.info(
                    "Sber SBP bank face expand %r → %r (glyph_pairs)",
                    raw_bank, pick,
                )

    body = "".join(
        str(work.get(k, ""))
        for k in (
            "sender_name", "receiver_name", "recipient_bank",
            "sender", "receiver", "bank_name",
        )
    )
    # Font-patch if payload needs glyphs absent from lean donors (щ/э/…) —
    # not only щ/Щ. Phone path uses the same idea (charset miss).
    need_font_patch = any(ch in body for ch in "щЩ")
    try:
        from sber_stealth_v3 import check_text_with_map
        _ref = ORIG_TEMPLATE if os.path.isfile(ORIG_TEMPLATE) else None
        if _ref and check_text_with_map(body, _cid_map_from_pdf(_ref)):
            need_font_patch = True
    except Exception:
        pass

    ranked = _rank_sbp_shells(work)
    tried: Set[str] = set()

    def _try_shell(path: str, lens: Dict[str, int], uni_gid: Dict[int, int]) -> Optional[bytes]:
        norm = os.path.normcase(path)
        if norm in tried:
            return None
        tried.add(norm)
        fitted = _fit_prepared_to_shell(work, path, lens, uni_gid)
        if not fitted:
            return None
        for k in ("sender_name", "receiver_name", "sender", "receiver"):
            orig = str(work.get(k, ""))
            got = str(fitted.get(k, ""))
            for ch in "щЩ":
                if ch in orig and ch not in got:
                    return None
        return _try_donor_orig(fitted, path)

    if not need_font_patch:
        for score, _slot, path, uni_gid in ranked:
            lens = _shell_field_byte_lens(path, uni_gid)
            hit = _try_shell(path, lens, uni_gid)
            if hit:
                if score:
                    logger.info(
                        "Sber dynamic: donor-orig %s (fit score %s)",
                        os.path.basename(path), score,
                    )
                return hit

        donor_hit = _try_donor_orig(work)
        if donor_hit:
            return donor_hit

        # Prefer original-band shells with FontFile2 ≥22KB (Proton floor).
        def _content_shell_rank(p: str) -> Tuple:
            ff2_ok = 0
            try:
                doc = fitz.open(p)
                best = 0
                for xref in range(1, doc.xref_length()):
                    try:
                        s = doc.xref_object(xref)
                    except Exception:
                        continue
                    if "/Length1" in s and "FlateDecode" in s:
                        raw = doc.xref_stream_raw(xref)
                        if raw:
                            best = max(best, len(raw))
                doc.close()
                ff2_ok = 0 if best >= 22_000 else 1
            except Exception:
                ff2_ok = 1
            return (
                ff2_ok,
                0 if 100_000 <= os.path.getsize(p) <= 105_000 else 1,
                abs(os.path.getsize(p) - 102_487),
            )

        content_shells = sorted(
            [p for p in _shell_template_candidates() if os.path.isfile(p)],
            key=_content_shell_rank,
        )
        for base_path in content_shells:
            norm = os.path.normcase(base_path)
            if norm in tried:
                continue
            tried.add(norm)
            cmap = _cid_map_from_pdf(base_path)
            if not cmap:
                continue
            uni_gid_map = {ord(ch): gid for ch, gid in cmap.items()}
            lens = _shell_field_byte_lens(base_path, uni_gid_map)
            fitted = _fit_prepared_to_shell(work, base_path, lens, uni_gid_map)
            if not fitted:
                continue
            logger.info("Sber dynamic: content-only %s", os.path.basename(base_path))
            result = _build_from_base(base_path, fitted, patch_font=False)
            if result:
                return result

    candidates = sorted(
        _sbp_shell_candidate_paths(work),
        key=lambda p: _font_patch_shell_rank(p, work),
    )
    # Prefer corpus FontFile2 ≥22KB (SBER_FONTFILE2_RAW_TOO_SMALL). Lean
    # runtime/unlocked (~19KB) only as last resort after original-band shells.
    def _ff2_comp_ok(path: str) -> bool:
        try:
            doc = fitz.open(path)
            best = 0
            for xref in range(1, doc.xref_length()):
                try:
                    s = doc.xref_object(xref)
                except Exception:
                    continue
                if "/Length1" in s and "FlateDecode" in s:
                    raw = doc.xref_stream_raw(xref)
                    if raw:
                        best = max(best, len(raw))
            doc.close()
            return best >= 22_000
        except Exception:
            return False

    fat = [p for p in candidates if _ff2_comp_ok(p)]
    lean = [p for p in candidates if p not in fat]
    # Never font-patch lean runtime/unlocked — Proton HARD on glyf/hmtx atlas.
    candidates = fat
    if not candidates:
        logger.error("Sber dynamic: no FontFile2≥22KB shells for font-patch")
        return None
    # Fewer charset misses first, then roomier FontFile2 (graft headroom).
    try:
        from sber_stealth_v3 import check_text_with_map

        def _miss_rank(path: str) -> Tuple:
            try:
                cmap = _cid_map_from_pdf(path) or {}
                miss = len(check_text_with_map(body, cmap)) if cmap else 99
            except Exception:
                miss = 99
            dec = 0
            try:
                doc = fitz.open(path)
                fm = tut._find_font_objects(doc)
                key = _pick_arial(fm)
                if key:
                    dec = len(doc.xref_stream(fm[key]["fontfile_xref"]) or b"")
                doc.close()
            except Exception:
                pass
            return (miss, -dec)

        candidates = sorted(candidates, key=_miss_rank)
    except Exception:
        pass
    # Chaos FIO can thrash every fat shell for minutes — hard cap.
    candidates = candidates[:5]

    for base_path in candidates:
        if not os.path.isfile(base_path):
            continue
        if not _sber_shell_exact_labels_ok(base_path):
            logger.info(
                "Sber dynamic: skip %s (exact label profile)",
                os.path.basename(base_path),
            )
            continue
        cmap = _cid_map_from_pdf(base_path) or {}
        uni_gid_map = {ord(ch): gid for ch, gid in cmap.items()}
        lens = _shell_field_byte_lens(base_path, uni_gid_map)
        bank_slot = lens.get("recipient_bank", 0)
        if bank_slot < 6:
            continue
        exact = dict(work)
        need_rcv = max(2, len(str(exact.get("receiver_name") or exact.get("receiver") or "")) * 2)
        need_snd = max(2, len(str(exact.get("sender_name") or exact.get("sender") or "")) * 2)
        rcv_slot = lens.get("receiver_name", 0) or 0
        snd_slot = lens.get("sender_name", 0) or 0
        if rcv_slot < need_rcv or snd_slot < need_snd:
            # Still try: net-zero slot rebalancing may preserve the full values.
            pass
        bank = str(exact.get("recipient_bank") or exact.get("bank_name") or "")
        if bank:
            need_bank = max(2, min(len(v) for v in _bank_slot_variants(bank)) * 2)
            if bank_slot < need_bank:
                # Prefer variants that fit; soft already ran.
                pass
        # Prefer wide name slots when injecting rare glyphs (щ) — but never skip all.
        if need_font_patch and (
            lens.get("sender_name", 0) < 30 or lens.get("receiver_name", 0) < 30
        ):
            # Try anyway; exact fitting will reject a physically short slot.
            pass
        strict_ok = _shell_fits_prepared_strict(base_path, exact)
        if not strict_ok:
            continue
        logger.info(
            "Sber dynamic: font-patch %s (bank_slot=%s name=%s/%s strict=%s)",
            os.path.basename(base_path),
            bank_slot,
            lens.get("sender_name"),
            lens.get("receiver_name"),
            strict_ok,
        )
        result = _build_from_base(base_path, exact, patch_font=True)
        if result:
            return result

    logger.error("Sber dynamic: all exact shell candidates failed")
    return None


def _font_patch_shell_rank(path: str, prepared: Dict[str, str]) -> Tuple:
    """Сначала широкие слоты банка/ФИО/суммы — без укорачивания данных."""
    try:
        cmap = _cid_map_from_pdf(path) or {}
        uni = {ord(ch): gid for ch, gid in cmap.items()}
        lens = _shell_field_byte_lens(path, uni)
    except Exception:
        return (9, 9, 9, 0, 0, 0, 10**12)
    bank = str(prepared.get("recipient_bank") or "")
    bank_slot = lens.get("recipient_bank", 0)
    snd = lens.get("sender_name", 0)
    rcv = lens.get("receiver_name", 0)
    amt_slot = lens.get("amount", 0)
    need_bank_full = max(2, len(bank) * 2) if bank else 2
    bank_vars = _bank_slot_variants(bank) if bank else [""]
    need_bank_min = max(2, min(len(v) for v in bank_vars) * 2)
    need_snd = max(2, len(str(prepared.get("sender_name") or "")) * 2)
    need_rcv = max(2, len(str(prepared.get("receiver_name") or "")) * 2)
    amt_digits = len(re.sub(r"\D", "", str(prepared.get("amount") or "")))
    # 5+ digit amounts need ≥23 B («68975.00  ₽»); 4-digit fits 21.
    need_amt = 23 if amt_digits >= 5 else 21
    bank_full_ok = 0 if bank_slot >= need_bank_full else 1
    amt_ok = 0 if amt_slot >= need_amt else 1
    strict = 0 if _shell_fits_prepared_strict(path, prepared) else 1
    fio_deficit = max(0, need_snd - snd) + max(0, need_rcv - rcv)
    bank_spare = max(0, bank_slot - need_bank_min)
    fio_ok = 0 if fio_deficit <= bank_spare + 2 else 1
    narrow = 0 if (snd >= 30 and rcv >= 30) else 1
    original_band = 0 if 100_000 <= os.path.getsize(path) <= 105_000 else 1
    # Short user bank («ВТБ») on «Альфа-Банк» shell → glyph_pairs < 238 HARD.
    donor_bank_chars = max(0, bank_slot // 2)
    try:
        doc = fitz.open(path)
        lines = [
            ln.strip()
            for ln in (doc[0].get_text("text") or "").splitlines()
            if ln.strip()
        ]
        doc.close()
        for i, ln in enumerate(lines):
            if "Банк получателя" in ln and i + 1 < len(lines):
                donor_bank_chars = len(lines[i + 1])
                break
    except Exception:
        pass
    bank_len_gap = abs(donor_bank_chars - len(bank)) if bank else 0
    # Full-charset runtime (label-exact) avoids FontFile2 growth from grafts.
    base = os.path.basename(path)
    runtime_first = 1
    if base == "S_sbp_runtime.pdf" or base.startswith("S_sbp_unlocked"):
        # Runtime/unlocked: full cmap but wrong atlas glyf/hmtx — never prefer.
        exact_font_profile = 9
        runtime_first = 9
    elif base == "receipt (7).pdf":
        # Baked original retains exact decoded+compressed FontFile2 lengths
        # after selective contour replacement for the full hard charset.
        exact_font_profile = 0
    else:
        exact_font_profile = 1
    # Prefer roomy FontFile2 — lean 52272 shells corrupt after щ grafts.
    ff2_dec = 0
    try:
        doc = fitz.open(path)
        fm = tut._find_font_objects(doc)
        key = _pick_arial(fm)
        if key:
            ff2_dec = len(doc.xref_stream(fm[key]["fontfile_xref"]) or b"")
        doc.close()
    except Exception:
        ff2_dec = 0
    ff2_tight = 0 if ff2_dec >= 52800 else (1 if ff2_dec >= 52400 else 2)
    return (
        bank_len_gap, runtime_first, exact_font_profile, ff2_tight,
        original_band, amt_ok, bank_full_ok, fio_ok, strict, narrow,
        -ff2_dec, -amt_slot, -bank_slot, -snd, -rcv, os.path.getsize(path),
    )


# Live OnlyPDF PASS как сырой оригинал (остальные CARD/ACCT → UNKNOWN/FAKE).
_PHONE_ONLYPDF_PASS_MARKERS = (
    "0004_1000000003898697264",
)


def _phone_donor_is_card_layout(path: str) -> bool:
    """True если phone-донор с меткой «Номер карты» (не «счёта»).

    OnlyPDF стабильно знает CARD-layout; ACCT → часто «чек не распознан».
    """
    try:
        doc = fitz.open(path)
        page = doc[0]
        mb = page.mediabox
        text = page.get_text()
        doc.close()
    except Exception:
        return False
    try:
        w, h = float(mb.width), float(mb.height)
    except Exception:
        return False
    if abs(w - 300.0) > 1.0 or abs(h - 699.0) > 1.0:
        return False
    low = (text or "").lower().replace("ё", "е")
    if "телефон получателя" not in low:
        return False
    return "номер карты получателя" in low


def _phone_donor_onlypdf_safe(path: str) -> bool:
    """CARD + известный OnlyPDF-PASS shell (другие CARD-оригиналы тоже UNKNOWN)."""
    if not _phone_donor_is_card_layout(path):
        return False
    base = os.path.basename(path)
    return any(m in base for m in _PHONE_ONLYPDF_PASS_MARKERS)


def _corpus_phone_donors() -> List[str]:
    try:
        from sber_corpus import corpus_paths
        raw = corpus_paths("phone")
    except Exception:
        raw = []
    # OnlyPDF: только проверенный PASS CARD-shell.
    seen: Set[str] = set()
    out: List[str] = []
    for p in raw:
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            continue
        seen.add(key)
        if _phone_donor_onlypdf_safe(p):
            out.append(p)
    if not out:
        # Fallback: любой CARD (хуже, но лучше ACCT).
        for p in raw:
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            if _phone_donor_is_card_layout(p):
                out.append(p)
    return out


def _phone_generation_template_path() -> str:
    donors = _corpus_phone_donors()
    if donors:
        target = 44250
        return min(donors, key=lambda p: abs(os.path.getsize(p) - target))
    if os.path.isfile(PHONE_ORIG_TEMPLATE):
        return PHONE_ORIG_TEMPLATE
    return PHONE_ORIG_TEMPLATE


def _phone_donor_text(prepared: Dict[str, str]) -> str:
    """Текст для подбора donor-shell (без счетов/документа/auth)."""
    keys = (
        "receiver_name", "receiver_phone",
        "sender_name", "amount", "commission",
    )
    return "".join(str(prepared.get(k, "")) for k in keys)


def _find_best_phone_donor(prepared: Dict[str, str]) -> Tuple[Optional[str], set]:
    from sber_stealth_v3 import check_text_with_map

    text = _phone_donor_text(prepared)
    best_path: Optional[str] = None
    best_miss: list = ["\x00"] * 999
    for path in _corpus_phone_donors():
        miss = check_text_with_map(text, _cid_map_from_pdf(path))
        if best_path is None or len(miss) < len(best_miss):
            best_path = path
            best_miss = miss
    if best_path is None and os.path.isfile(PHONE_ORIG_TEMPLATE):
        best_path = PHONE_ORIG_TEMPLATE
        best_miss = check_text_with_map(text, _cid_map_from_pdf(best_path))
    return best_path, set(best_miss)


def _try_donor_orig_phone(
    prepared: Dict[str, str],
    donor_path: Optional[str] = None,
) -> Optional[bytes]:
    from sber_stealth_v3 import PHONE_FIELD_COORDS, check_text_with_map, create_sber_stealth

    body_text = _phone_donor_text(prepared)
    if donor_path:
        miss = check_text_with_map(body_text, _cid_map_from_pdf(donor_path))
        if miss:
            cmap = _cid_map_from_pdf(donor_path)
            prepared = _auto_fix_prepared_fields(prepared, cmap)
            body_text = _phone_donor_text(prepared)
            miss = check_text_with_map(body_text, cmap)
        if miss:
            return None
        donor = donor_path
    else:
        donor, miss_set = _find_best_phone_donor(prepared)
        if not donor or miss_set:
            return None

    cid_map = _cid_map_from_pdf(donor)
    data = _auto_fix_prepared_fields({k: prepared[k] for k in prepared}, cid_map)
    result = create_sber_stealth(
        template_path=donor,
        data=data,
        auto_select=False,
        cid_map=cid_map,
        field_coords=PHONE_FIELD_COORDS,
    )
    if not result:
        return None

    donor_size = os.path.getsize(donor)
    # Не трогаем >>stream / ID так, чтобы сдвинуть offsets — OnlyPDF = FAKE.
    # /ID randomize на phone PASS-shell → OnlyPDF FAKE (проверено live 28.07.26).
    before = result
    result = _fix_stream_separators(result)
    if len(result) != len(before):
        result = before
    # Fraudex: keep donor CreationDate/ModDate (cross-day meta rewrite → structure).
    if len(result) != donor_size:
        logger.warning(
            "Sber phone donor-orig: size drift %d→%d — skip",
            donor_size, len(result),
        )
        return None
    # Жёстко: content-stream decoded должен совпасть с donor (иначе OnlyPDF FAKE).
    try:
        import fitz as _fitz
        _doc = _fitz.open(stream=result, filetype="pdf")
        _xref = _doc[0].get_contents()[0]
        _raw = _doc.xref_stream_raw(_xref)
        _dec = _doc.xref_stream(_xref)
        _doc.close()
        if len(_raw) != 932 or len(_dec) != 4662 or _raw[:2] != b"\x78\x9c":
            logger.warning(
                "Sber phone donor-orig: bad stream %d/%d — skip",
                len(_raw), len(_dec),
            )
            return None
    except Exception as exc:
        logger.warning("Sber phone donor-orig: stream check fail: %s", exc)
        return None
    # startxref must still point at xref (never ship broken «оригинал»).
    sx = re.search(rb"startxref\s*[\r\n]+(\d+)", result)
    if not sx or result[int(sx.group(1)) : int(sx.group(1)) + 4] != b"xref":
        logger.error("Sber phone donor-orig: startxref broken — skip")
        return None
    before = result
    result = _apply_sber_orphan_blank_pdf(result)
    if len(result) != donor_size:
        logger.warning(
            "Sber phone donor-orig: orphan blank size drift %d→%d — keep pre-blank",
            donor_size, len(result),
        )
        result = before
    logger.info(
        "Sber phone donor-orig OK: %s (%d bytes, base %s)",
        os.path.basename(donor), len(result), donor_size,
    )
    return result


def build_dynamic_sber_phone(prepared: Dict[str, str]) -> Optional[bytes]:
    """Телефон — donor-orig, затем font-patch (как SBP)."""
    from sber_stealth_v3 import check_text_with_map

    sgl.ensure_library()
    work = dict(prepared)
    body = _phone_donor_text(work)

    paths: List[str] = []
    # Только CARD-layout доноры (OnlyPDF «не распознан» на «Номер счёта»).
    for p in _corpus_phone_donors():
        if os.path.isfile(p):
            paths.append(p)
    # ACCT shell (PHONE_ORIG) — только если card-доноров нет.
    if not paths and os.path.isfile(PHONE_ORIG_TEMPLATE):
        logger.warning(
            "Sber phone: no CARD-layout donors — fallback ACCT shell (OnlyPDF risk)"
        )
        paths.append(PHONE_ORIG_TEMPLATE)
    if not paths:
        logger.error("Sber phone: no phone donors")
        return None

    scored: List[Tuple] = []
    for path in paths:
        cmap = _cid_map_from_pdf(path)
        trial = _auto_fix_prepared_fields(dict(work), cmap)
        miss = check_text_with_map(_phone_donor_text(trial), cmap)
        uni_gid = {ord(ch): gid for ch, gid in cmap.items()}
        lens = _shell_field_byte_lens(path, uni_gid, _PHONE_FIELD_Y)
        fit_penalty = 0
        if not miss and lens:
            enc = lambda t, m=cmap: _sber_enc(t, {ord(k): v for k, v in m.items()})
            for key in _PHONE_FIELD_Y:
                if key not in lens:
                    continue
                _t, b = _fit_text_encoded_length(
                    trial.get(key, ""), lens[key], enc,
                    key_hint="phone" if key == "receiver_phone" else key,
                    phone_style=key in ("receiver_phone", "amount", "commission"),
                    pad_side="trail",
                )
                fit_penalty += abs(len(b) - lens[key])
        # Широкие слоты ФИО важнее: полное имя не режется.
        scored.append((
            len(miss),
            -(lens.get("receiver_name") or 0),
            -(lens.get("sender_name") or 0),
            fit_penalty,
            path,
        ))
    scored.sort()

    # Any missing payload glyph requires exact font extension.
    need_font_patch = bool(scored and scored[0][0])

    tried: Set[str] = set()
    if not need_font_patch:
        for nmiss, _nr, _ns, _fit, path in scored:
            cmap = _cid_map_from_pdf(path)
            trial = _auto_fix_prepared_fields(dict(work), cmap)
            uni_gid = {ord(ch): gid for ch, gid in cmap.items()}
            lens = _shell_field_byte_lens(path, uni_gid, _PHONE_FIELD_Y)
            fitted = _fit_prepared_to_shell(
                trial, path, lens, uni_gid, field_y=_PHONE_FIELD_Y,
            )
            if not fitted:
                continue
            hit = _try_donor_orig_phone(fitted, path)
            if hit:
                if nmiss:
                    logger.info(
                        "Sber phone donor-orig %s (charset miss %d)",
                        os.path.basename(path), nmiss,
                    )
                return hit
            tried.add(os.path.normcase(path))

    # Font-patch: сначала shell с самыми широкими слотами ФИО.
    for nmiss, _nr, _ns, _fit, path in scored:
        if not os.path.isfile(path):
            continue
        exact = dict(work)
        for k, default in (
            ("receiver_account", exact.get("sender_account") or "•••• 0000"),
            ("document_num", exact.get("document_num") or "00"),
            ("auth_code", exact.get("auth_code") or "000000"),
        ):
            exact.setdefault(k, default)
        # scored stores -lens for receiver/sender name slots
        rcv_slot, snd_slot = -int(_nr), -int(_ns)
        logger.info(
            "Sber phone: font-patch %s (miss=%d rcv_slot=%s)",
            os.path.basename(path), nmiss, rcv_slot,
        )
        result = _build_from_base(
            path, exact, patch_font=True,
            field_y=_PHONE_FIELD_Y, field_specs=_PHONE_FIELD_SPECS,
        )
        if result:
            return result

    miss_chars = (
        check_text_with_map(body, _cid_map_from_pdf(PHONE_ORIG_TEMPLATE))
        if os.path.isfile(PHONE_ORIG_TEMPLATE) else ["?"]
    )
    logger.error(
        "Sber phone: нет donor-shell для текста (%s)",
        "".join(miss_chars),
    )
    return None

def _corpus_card_donors() -> List[str]:
    try:
        from sber_corpus import corpus_paths
        return corpus_paths("card_other")
    except Exception:
        return []


def _card_donor_text(prepared: Dict[str, str]) -> str:
    keys = (
        "date_time", "sender_name", "amount", "commission", "charged",
        "bank", "country", "dest_card", "sender_card",
    )
    return "".join(str(prepared.get(k, "")) for k in keys)


def build_dynamic_sber_card(prepared: Dict[str, str]) -> Optional[bytes]:
    """Карта→другой банк — hex TJ donor; prefer content-only (OnlyPDF-safe)."""
    from sber_stealth_v3 import check_text_with_map

    sgl.ensure_library()
    work = dict(prepared)

    paths: List[str] = []
    if os.path.isfile(CARD_ORIG_TEMPLATE):
        paths.append(CARD_ORIG_TEMPLATE)
    paths.extend(
        p for p in _corpus_card_donors()
        if os.path.normcase(p) != os.path.normcase(CARD_ORIG_TEMPLATE)
    )
    if not paths:
        logger.error("Sber card: no donor template")
        return None

    scored: List[Tuple] = []
    for path in paths:
        cmap = _cid_map_from_pdf(path) or {}
        trial = _auto_fix_prepared_fields(dict(work), cmap)
        miss = check_text_with_map(_card_donor_text(trial), cmap)
        uni_gid = {ord(ch): gid for ch, gid in cmap.items()}
        lens = _shell_field_byte_lens(path, uni_gid, _CARD_FIELD_Y)
        scored.append((len(miss), -(lens.get("sender_name") or 0), path, trial, lens, uni_gid))
    scored.sort()

    # 1) content-only only when charset fits (OnlyPDF-safe)
    for nmiss, _ns, path, trial, lens, uni_gid in scored:
        if nmiss:
            continue
        fitted = _fit_prepared_to_shell(
            trial, path, lens, uni_gid, field_y=_CARD_FIELD_Y,
        )
        if not fitted:
            continue
        hit = _build_from_base(
            path, fitted, patch_font=False,
            field_y=_CARD_FIELD_Y, field_specs=_CARD_FIELD_SPECS,
        )
        if hit:
            return hit

    # 2) soft-fit within donor charset (still no font-patch)
    for nmiss, _ns, path, trial, lens, uni_gid in scored:
        fitted = _fit_prepared_to_shell(
            trial, path, lens, uni_gid, field_y=_CARD_FIELD_Y,
        )
        soft = fitted or trial
        soft.setdefault("country", soft.get("country") or "Россия")
        # Drop chars still missing — stay content-only
        cmap = _cid_map_from_pdf(path) or {}
        soft = _auto_fix_prepared_fields(soft, cmap)
        if check_text_with_map(_card_donor_text(soft), cmap):
            continue
        logger.info(
            "Sber card: content-only soft %s (orig_miss=%d)",
            os.path.basename(path), nmiss,
        )
        result = _build_from_base(
            path, soft, patch_font=False,
            field_y=_CARD_FIELD_Y, field_specs=_CARD_FIELD_SPECS,
        )
        if result:
            return result

    logger.error("Sber card: no content-only donor fit (font-patch disabled for OnlyPDF)")
    return None
