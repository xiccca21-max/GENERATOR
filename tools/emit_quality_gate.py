# -*- coding: utf-8 -*-
"""Pre-send quality gate for bot + generators.

Hard-rejects:
  - Sber odd Tj (structural OnlyPDF fingerprint)
  - Visual integrity: missing/tofu glyphs, clipped coordinates, mangled FIO
  - Payload match: amount / FIO / date / phone must appear on the face

Does not call Telegram.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.receipt_method_profiles import (  # noqa: E402
    FieldContract,
    MethodProfile,
    effective_size_band,
    get_profile,
)


def _pdf_literal_unescape(raw: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b != 0x5C or i + 1 >= n:
            out.append(b)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in b"01234567":
            j = i + 1
            val = 0
            cnt = 0
            while j < n and raw[j] in b"01234567" and cnt < 3:
                val = (val << 3) | (raw[j] - 48)
                j += 1
                cnt += 1
            out.append(val & 0xFF)
            i = j
            continue
        out.append(nxt)
        i += 2
    return bytes(out)


def count_odd_tj(pdf: bytes) -> Tuple[int, int]:
    """Return (odd_count, total_tj) over content streams.

    Ignores T-Bank /F3 ruble glyph `(i)Tj` (single-byte, present in every
    genuine corpus PDF) — that is not the Sber odd-Tj OnlyPDF fingerprint.
    """
    import fitz

    odd = 0
    total = 0
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        for xref in range(1, doc.xref_length()):
            try:
                s = doc.xref_stream(xref)
            except Exception:
                continue
            if not s:
                continue
            for m in re.finditer(rb"\((?:\\.|[^\\)])*\)\s*Tj", s):
                lit = m.group(0)
                inner = lit[1 : lit.rfind(b")Tj")]
                try:
                    unesc = _pdf_literal_unescape(inner)
                except Exception:
                    continue
                if unesc == b"i":
                    pre = s[max(0, m.start() - 120) : m.start()]
                    if b"/F3" in pre:
                        continue
                total += 1
                if len(unesc) % 2:
                    odd += 1
    finally:
        doc.close()
    return odd, total


def local_proton_verdict(pdf: bytes) -> Tuple[str, str]:
    """(verdict, detail). verdict: ЧИСТО|ФЕЙК|UNKNOWN|ERR."""
    try:
        checker = Path(r"C:\Users\fanis\OneDrive\Desktop\pdf-checker-bot")
        if checker.is_dir() and str(checker) not in sys.path:
            sys.path.insert(0, str(checker))
        # Prefer checker over zapaska vendor/detector
        sys.path = [p for p in sys.path if "vendor" not in p.replace("\\", "/").lower()]
        if str(checker) not in sys.path:
            sys.path.insert(0, str(checker))
        from detector import route as route_bank  # type: ignore

        _bank, res, _rec = route_bank(pdf)
        v = str(res.get("verdict") or "").upper()
        flags = res.get("flags") or []
        if flags:
            detail = " | ".join(str(f)[:100] for f in flags[:16])
        else:
            detail = v
        if v == "ФЕЙК":
            return "ФЕЙК", detail
        if "НЕИЗВЕСТ" in v:
            return "UNKNOWN", detail
        if v == "ЧИСТО":
            return "ЧИСТО", detail
        return v or "UNKNOWN", detail
    except Exception as exc:
        return "ERR", f"{type(exc).__name__}:{exc}"


_TOFU_RE = re.compile(r"[\ufffd□�]")
_INITIAL_ONLY_RE = re.compile(r"^[А-ЯA-Z]\.$")
_RU_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_RU_MONTH_TO_NUM = {m: i + 1 for i, m in enumerate(_RU_MONTHS)}


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _amount_in_text(digs: str, text: str) -> bool:
    if not digs or len(digs) < 2:
        return True
    text_digs = re.sub(r"\D", "", text or "")
    if digs in text_digs:
        return True
    if re.search(rf"(?<!\d){re.escape(digs)}(?:00)?(?!\d)", text_digs):
        return True
    if digs + "00" in text_digs:
        return True
    return False


def _parse_expect_date(expect: Dict[str, Any]) -> Optional[Tuple[int, int, int]]:
    """Return (day, month, year) from expect fields, or None."""
    dt = _norm_space(
        str(
            expect.get("date_time")
            or expect.get("new_date")
            or expect.get("date")
            or ""
        )
    )
    if not dt:
        return None
    if dt.lower() in ("сейчас", "now", "-", "авто", "auto"):
        return None  # must be expanded upstream before gate
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", dt)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        return d, mo, y
    m = re.search(
        r"(\d{1,2})\s+([А-Яа-яё]+)\s+(\d{4})",
        dt,
        re.IGNORECASE,
    )
    if m:
        mon = _RU_MONTH_TO_NUM.get(m.group(2).lower())
        if mon:
            return int(m.group(1)), mon, int(m.group(3))
    if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", dt):
        parts = dt.split(".")
        return int(parts[0]), int(parts[1]), int(parts[2])
    return None


def _date_in_text(text: str, expect: Dict[str, Any]) -> Tuple[bool, str]:
    parsed = _parse_expect_date(expect)
    if not parsed:
        return True, "skip"
    day, month, year = parsed
    text_n = _norm_space(text)
    text_l = text_n.lower()
    y = str(year)
    if y not in text_n:
        return False, f"visual-missing-year:{y}"
    mon_ru = _RU_MONTHS[month - 1]
    dotted = f"{day:02d}.{month:02d}.{year}"
    dotted2 = f"{day}.{month:02d}.{year}"
    # T-Bank often keeps dd.mm.yyyy; Sber uses «1 августа 2026»
    if dotted in text_n or dotted2 in text_n or f"{day:02d}/{month:02d}/{year}" in text_n:
        return True, "ok"
    if mon_ru in text_l:
        if re.search(rf"(?<!\d)0?{day}\s+{re.escape(mon_ru)}\s+{year}", text_l):
            return True, "ok"
        if not re.search(rf"(?<!\d)0?{day}\b", text_l):
            return False, f"visual-wrong-day:{day}"
        return True, "ok"
    if f"{day:02d}{month:02d}{year}" in re.sub(r"\D", "", text_n):
        return True, "ok"
    return False, f"visual-missing-date:{day:02d}.{month:02d}.{year}"


def _phone_in_text(text: str, phone: str) -> bool:
    digs = re.sub(r"\D", "", phone or "")
    if digs.startswith("8") and len(digs) == 11:
        digs = "7" + digs[1:]
    if len(digs) < 10:
        return True
    core = digs[-10:]
    text_digs = re.sub(r"\D", "", text or "")
    if core in text_digs or core[1:] in text_digs:
        return True
    # Alfa phone face is masked «918***6427» → digits collapse to DEF+last4.
    masked_core = core[:3] + core[-4:]
    if masked_core and masked_core in text_digs:
        return True
    plain = (text or "").replace("\u00a0", "").replace(" ", "")
    if f"{core[:3]}***{core[-4:]}" in plain:
        return True
    return False


def _fio_token_ok(token: str, text: str, *, allow_shorten: bool = False) -> bool:
    tok = (token or "").strip()
    if len(tok) < 2:
        return True
    if tok in text:
        return True
    if allow_shorten and len(tok) >= 4 and tok[:4] in text:
        return True
    return False


_AUTO_VALUES = {"", "-", "авто", "auto", "сейчас", "now"}
_EXPECT_ALIASES = {
    "date_time": ("date_time", "new_date", "date"),
    "sender": ("sender", "sender_name"),
    "receiver": ("receiver", "receiver_name", "recipient"),
    "phone": ("phone", "receiver_phone"),
    "bank": ("bank", "bank_name", "recipient_bank"),
    "amount": ("amount", "new_amount"),
    "account": ("account", "sender_account", "receiver_account"),
    "operation_num": ("operation_num", "receipt_num"),
    "receipt_num": ("receipt_num", "operation_num"),
    "sbp_id": ("sbp_id", "spb_number"),
    "receiver_card": ("receiver_card", "card"),
}


def _expect_value(expect: Dict[str, Any], key: str) -> str:
    for name in _EXPECT_ALIASES.get(key, (key,)):
        value = _norm_space(str(expect.get(name) or ""))
        if value:
            return value
    if key == "date_time":
        day = _norm_space(str(expect.get("date") or ""))
        tm = _norm_space(str(expect.get("time") or ""))
        return _norm_space(f"{day} {tm}")
    return ""


def _concrete(value: str) -> bool:
    return _norm_space(value).lower() not in _AUTO_VALUES


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _alnum(value: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", value or "").casefold()


def _amount_digits(value: str) -> str:
    raw = _norm_space(value)
    if not raw:
        return ""
    raw = re.sub(r"\s*(?:₽|RUR)\s*$", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+i\s*$", "", raw)
    # Payload amounts are overwhelmingly integer rubles.  Preserve a genuine
    # decimal fraction, but do not read grouped integer "75 670" as kopecks.
    compact = raw.replace("\xa0", " ").replace(" ", "")
    m = re.fullmatch(r"([+-]?\d+)(?:[.,](\d{1,2}))?", compact)
    if not m:
        return _digits(raw)
    whole = str(int(m.group(1)))
    frac = (m.group(2) or "").ljust(2, "0")
    return whole + (frac if frac and frac != "00" else "")


def _masked_display(value: str, mask: Optional[str]) -> str:
    if not mask:
        return _norm_space(value)
    if "*" in value or "•" in value:
        return _norm_space(value)
    digs = _digits(value)
    if mask == "card_6_6_4" and len(digs) >= 10:
        return f"{digs[:6]}******{digs[-4:]}"
    if mask == "card_last4" and len(digs) >= 4:
        return f"*{digs[-4:]}"
    if mask == "last4" and len(digs) >= 4:
        return f"**** {digs[-4:]}"
    if mask == "tbank_account" and len(digs) >= 16:
        return f"{digs[:12]}****{digs[-4:]}"
    if mask == "alfa_account" and len(digs) >= 10:
        return f"{digs[:6]}**********{digs[-4:]}"
    if mask == "alfa_phone" and len(digs) >= 10:
        core = digs[-10:]
        return f"{core[:3]}***{core[-4:]}"
    if mask == "alfa_fio":
        parts = [p for p in _norm_space(value).split(" ") if p]
        if not parts:
            return ""
        first = parts[0]
        masked = first if len(first) < 5 else f"{first[:3]}**{first[-1]}"
        initials = [f"{p[0]}." for p in parts[1:] if p]
        return " ".join([masked, *initials])
    return _norm_space(value)


def _display_variants(value: str, mask: Optional[str]) -> Tuple[str, ...]:
    shown = _masked_display(value, mask)
    variants = [shown]
    if mask == "last4":
        digs = _digits(value)
        if len(digs) >= 4:
            variants.extend((f"•• {digs[-4:]}", f"****{digs[-4:]}", f"••{digs[-4:]}"))
    # Sber SBP face: «+7 995 …» without parens; bot payload often «+7 (995) …».
    digs = _digits(value)
    if len(digs) >= 10 and (mask is None or mask == ""):
        core = digs[-10:]
        variants.extend(
            (
                f"+7 {core[:3]} {core[3:6]}-{core[6:8]}-{core[8:10]}",
                f"+7 ({core[:3]}) {core[3:6]}-{core[6:8]}-{core[8:10]}",
                f"+7{core}",
            )
        )
    return tuple(dict.fromkeys(variants))


def _exact_text_present(text: str, expected: str) -> bool:
    hay = _norm_space(text).casefold()
    needle = _norm_space(expected).casefold()
    if not needle:
        return False
    return re.search(
        rf"(?<![0-9a-zа-яё]){re.escape(needle)}(?![0-9a-zа-яё])",
        hay,
        re.IGNORECASE,
    ) is not None


def _exact_compact_present(text: str, expected: str, *, alnum: bool = False) -> bool:
    needle = _alnum(expected) if alnum else _digits(expected)
    if not needle:
        return False
    if alnum:
        # Long operation/SBP identifiers may be intentionally split across two
        # visual lines.  Removing separators still compares the complete value;
        # a prefix or suffix alone cannot satisfy this check.
        return needle in _alnum(text)
    compact = _alnum if alnum else _digits
    for line in (text or "").splitlines():
        hay = compact(line)
        # Labels may share the extracted line, but no extra compact payload may
        # trail the expected value.
        if hay == needle or hay.endswith(needle):
            return True
    return False


def _datetime_exact(text: str, expect: Dict[str, Any]) -> Tuple[bool, str]:
    raw = _expect_value(expect, "date_time")
    if not _concrete(raw):
        return False, "profile-unresolved-date"
    parsed = _parse_expect_date(expect)
    if not parsed:
        return False, "profile-invalid-date"
    day, month, year = parsed
    compact = _norm_space(text)
    date_ok = any(
        candidate in compact
        for candidate in (
            f"{day:02d}.{month:02d}.{year}",
            f"{day}.{month:02d}.{year}",
            f"{day:02d}/{month:02d}/{year}",
        )
    )
    month_form = rf"(?<!\d)0?{day}\s+{re.escape(_RU_MONTHS[month - 1])}\s+{year}(?!\d)"
    if not date_ok and not re.search(month_form, compact, re.IGNORECASE):
        return False, f"exact-date:{day:02d}.{month:02d}.{year}"
    tm = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", raw)
    if tm:
        expected_time = f"{int(tm.group(1)):02d}:{tm.group(2)}"
        if tm.group(3) is not None:
            expected_time += f":{tm.group(3)}"
        if expected_time not in compact:
            return False, f"exact-time:{expected_time}"
    return True, "ok"


def _amount_occurrences(text: str, expected: str) -> List[str]:
    want = _amount_digits(expected)
    found: List[str] = []
    currency_re = re.compile(
        r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})*|\d+)(?:[.,](\d{2}))?"
        r"\s*(?:₽|RUR|(?<![A-Za-z])i(?![A-Za-z]))"
    )
    for match in currency_re.finditer(text):
        whole = str(int(_digits(match.group(1)) or "0"))
        frac = match.group(2) or ""
        value = whole + (frac if frac and frac != "00" else "")
        if value == want:
            found.append(match.group(0))
    return found


def _field_exact(
    text: str,
    expect: Dict[str, Any],
    name: str,
    contract: FieldContract,
) -> Tuple[bool, str]:
    value = _expect_value(expect, name)
    if not _concrete(value):
        if contract.required:
            return False, f"profile-missing-expect:{name}"
        return True, "skip"
    variants = _display_variants(value, contract.display_mask)
    shown = variants[0]
    if contract.kind == "amount":
        count = len(_amount_occurrences(text, shown))
        if count < contract.repeats:
            return False, f"exact-{name}:{count}/{contract.repeats}"
        return True, "ok"
    if contract.kind in ("phone", "card", "account"):
        if any("*" in candidate or "•" in candidate for candidate in variants):
            hay = _norm_space(text).replace(" ", "")
            if not any(_norm_space(candidate).replace(" ", "") in hay for candidate in variants):
                return False, f"exact-{name}:{shown}"
            return True, "ok"
        if not any(_exact_compact_present(text, candidate) for candidate in variants):
            return False, f"exact-{name}:{shown}"
        return True, "ok"
    if contract.kind == "id":
        if not _exact_compact_present(text, shown, alnum=True):
            return False, f"exact-{name}:{shown}"
        return True, "ok"
    if contract.kind == "datetime":
        return _datetime_exact(text, expect)
    if not _exact_text_present(text, shown):
        return False, f"exact-{name}:{shown}"
    return True, "ok"


def _xref_number(value: Tuple[str, str]) -> int:
    if not value or value[0] != "xref":
        return 0
    m = re.search(r"\d+", value[1] or "")
    return int(m.group(0)) if m else 0


def _xref_numbers(value: Tuple[str, str]) -> List[int]:
    if not value or value[0] not in ("xref", "array"):
        return []
    return [int(n) for n in re.findall(r"(\d+)\s+0\s+R", value[1] or "")]


def _decode_utf16_hex(raw: bytes) -> str:
    try:
        if len(raw) % 2:
            raw = b"\x00" + raw
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def _parse_tounicode(stream: bytes) -> Dict[int, str]:
    mapping: Dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", stream, re.DOTALL):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                mapping[int(src, 16)] = _decode_utf16_hex(bytes.fromhex(dst.decode("ascii")))
            except (ValueError, UnicodeDecodeError):
                continue
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", stream, re.DOTALL):
        for start, end, dst in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>",
            block,
        ):
            try:
                lo, hi, base = int(start, 16), int(end, 16), int(dst, 16)
                width = max(2, len(dst) // 2)
                for offset, cid in enumerate(range(lo, min(hi, lo + 4095) + 1)):
                    mapping[cid] = _decode_utf16_hex((base + offset).to_bytes(width, "big"))
            except (ValueError, OverflowError):
                continue
        for start, end, array in re.findall(
            rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]",
            block,
            re.DOTALL,
        ):
            try:
                lo, hi = int(start, 16), int(end, 16)
                values = re.findall(rb"<([0-9A-Fa-f]+)>", array)
                for cid, dst in zip(range(lo, min(hi, lo + 4095) + 1), values):
                    mapping[cid] = _decode_utf16_hex(bytes.fromhex(dst.decode("ascii")))
            except (ValueError, UnicodeDecodeError):
                continue
    return mapping


def _font_sanity(doc, page, expected_chars: Set[str]) -> Tuple[bool, str]:
    try:
        from fontTools.ttLib import TTFont
    except Exception as exc:
        return False, f"fonttools-unavailable:{type(exc).__name__}"

    unicode_mapped: Set[str] = set()
    contour_mapped: Set[str] = set()
    healthy_fonts = 0
    for item in page.get_fonts(full=True):
        xref = int(item[0])
        font_type = str(item[2] or "")
        if xref <= 0 or font_type != "Type0":
            continue
        tu_xref = _xref_number(doc.xref_get_key(xref, "ToUnicode"))
        if not tu_xref:
            return False, f"font-missing-tounicode:{xref}"
        try:
            cmap = _parse_tounicode(doc.xref_stream(tu_xref) or b"")
        except Exception as exc:
            return False, f"font-bad-tounicode:{xref}:{type(exc).__name__}"
        if not cmap:
            return False, f"font-empty-tounicode:{xref}"
        # T-Bank binds orphan F1 glyphs to off-page PUA markers in ToUnicode —
        # that is intentional (not visible on face). Visible PUA is checked later.
        unicode_mapped.update(
            ch for value in cmap.values() for ch in value
            if not (0xE000 <= ord(ch) <= 0xF8FF)
        )

        descendants = _xref_numbers(doc.xref_get_key(xref, "DescendantFonts"))
        if not descendants:
            return False, f"font-no-descendant:{xref}"
        descendant = descendants[0]
        descriptor = _xref_number(doc.xref_get_key(descendant, "FontDescriptor"))
        if not descriptor:
            return False, f"font-no-descriptor:{xref}"
        ff2_xref = _xref_number(doc.xref_get_key(descriptor, "FontFile2"))
        if not ff2_xref:
            # CFF fonts are valid, but still require an embedded FontFile3.
            ff3_xref = _xref_number(doc.xref_get_key(descriptor, "FontFile3"))
            if not ff3_xref:
                return False, f"font-not-embedded:{xref}"
            healthy_fonts += 1
            continue
        try:
            raw = doc.xref_stream(ff2_xref) or b""
            font = TTFont(BytesIO(raw), lazy=False, recalcBBoxes=False, recalcTimestamp=False)
            order = font.getGlyphOrder()
            if len(order) < 2 or "maxp" not in font:
                return False, f"font-empty-ttf:{xref}"
            glyf = font["glyf"] if "glyf" in font else None
            if glyf is not None:
                nonempty = 0
                for glyph_name in order[1:]:
                    glyph = glyf[glyph_name]
                    if getattr(glyph, "numberOfContours", 0) != 0 or getattr(glyph, "components", None):
                        nonempty += 1
                if nonempty < 3:
                    return False, f"font-contours-empty:{xref}"
                cid_map_key = doc.xref_get_key(descendant, "CIDToGIDMap")
                cid_map_stream = b""
                cid_map_xref = _xref_number(cid_map_key)
                if cid_map_xref:
                    cid_map_stream = doc.xref_stream(cid_map_xref) or b""

                def cid_to_gid(cid: int) -> int:
                    if cid_map_stream:
                        offset = cid * 2
                        if offset + 2 > len(cid_map_stream):
                            return -1
                        return int.from_bytes(cid_map_stream[offset : offset + 2], "big")
                    return cid

                for cid, mapped_text in cmap.items():
                    if len(mapped_text) != 1:
                        continue
                    ch = mapped_text
                    if ch not in expected_chars or not (ch.isalpha() or ch.isdigit()):
                        continue
                    gid = cid_to_gid(cid)
                    if gid <= 0 or gid >= len(order):
                        continue
                    glyph = glyf[order[gid]]
                    if (
                        getattr(glyph, "numberOfContours", 0) != 0
                        or getattr(glyph, "components", None)
                    ):
                        contour_mapped.add(ch)
            font.close()
            healthy_fonts += 1
        except Exception as exc:
            return False, f"font-invalid-ttf:{xref}:{type(exc).__name__}"

    if healthy_fonts < 1:
        return False, "font-no-healthy-embedded-font"
    missing = sorted(
        ch for ch in expected_chars
        if (ch.isalpha() or ch.isdigit()) and ch not in unicode_mapped
    )
    if missing:
        return False, f"font-cmap-missing:{''.join(missing[:12])}"
    missing_contours = sorted(
        ch for ch in expected_chars
        if (ch.isalpha() or ch.isdigit())
        and ch in unicode_mapped
        and ch not in contour_mapped
    )
    if missing_contours:
        return False, f"font-contour-missing:{''.join(missing_contours[:12])}"
    return True, "ok"


def _strict_anchor_check(page, profile: MethodProfile, expect: Dict[str, Any]) -> Tuple[bool, str]:
    lines = _span_lines(page)
    seen = set()
    unique_lines = []
    for line in lines:
        key = (line[0], round(line[1], 2), round(line[2], 2), round(line[3], 2))
        if key not in seen:
            seen.add(key)
            unique_lines.append(line)

    for name, contract in profile.fields.items():
        if not contract.anchor or contract.kind == "datetime":
            continue
        value = _expect_value(expect, name)
        if not _concrete(value):
            continue
        variants = _display_variants(value, contract.display_mask)
        shown = variants[0]
        if contract.kind == "amount":
            want = _amount_digits(shown)
            hits = [line for line in unique_lines if _amount_digits(line[0]) == want]
        elif contract.kind == "phone":
            want = _digits(value)[-10:]
            hits = [
                line for line in unique_lines
                if want and want in _digits(line[0])
            ]
            if not hits:
                visible = tuple(candidate.replace(" ", "") for candidate in variants)
                hits = [
                    line for line in unique_lines
                    if any(candidate in line[0].replace(" ", "") for candidate in visible)
                ]
        elif contract.kind in ("card", "account"):
            visible = tuple(candidate.replace(" ", "") for candidate in variants)
            hits = [
                line for line in unique_lines
                if any(candidate in line[0].replace(" ", "") for candidate in visible)
            ]
        else:
            hits = [line for line in unique_lines if _norm_space(shown) in line[0]]
            if not hits and contract.kind == "id":
                suffix = _alnum(shown)[-5:]
                hits = [line for line in unique_lines if suffix and suffix in _alnum(line[0])]
        if not hits:
            return False, f"anchor-missing:{name}"
        if profile.bank == "tbank" and contract.kind == "amount":
            if len(hits) < contract.repeats:
                return False, f"anchor-amount-count:{len(hits)}/{contract.repeats}"
            for hit in hits[:contract.repeats]:
                if not any(abs(hit[2] - target) <= 8.0 for target in (243.68, 237.77, 250.0)):
                    return False, f"anchor-tbank-{name}:x1={hit[2]:.1f}"
            continue
        anchor = profile.anchors[contract.anchor]
        edge, target, tolerance = anchor
        hit = hits[0]
        actual = hit[1] if edge == "left" else hit[2]
        if edge == "center":
            actual = (hit[1] + hit[2]) / 2.0
        if abs(actual - target) > tolerance:
            return False, f"anchor-{profile.method}-{name}:{edge}={actual:.1f}"

    parsed = _parse_expect_date(expect)
    if parsed:
        d, m, y = parsed
        date_hits = [
            line for line in unique_lines
            if (
                f"{d:02d}.{m:02d}.{y}" in line[0]
                or (_RU_MONTHS[m - 1] in line[0].lower() and str(y) in line[0])
            )
        ]
        if not date_hits:
            return False, "anchor-missing:date"
        edge, target, tolerance = profile.anchors["date"]
        hit = date_hits[0]
        actual = hit[1] if edge == "left" else hit[2]
        if edge == "center":
            actual = (hit[1] + hit[2]) / 2.0
        if abs(actual - target) > tolerance:
            return False, f"anchor-{profile.method}-date:{edge}={actual:.1f}"
    return True, "ok"


def _keywords_ok(doc, profile: MethodProfile, expect: Dict[str, Any]) -> Tuple[bool, str]:
    if not profile.keywords_by_face_date:
        return True, "skip"
    parsed = _parse_expect_date(expect)
    if not parsed:
        return False, "keywords-unresolved-face-date"
    face_date = date(parsed[2], parsed[1], parsed[0])
    expected = "991" if face_date < date(2026, 7, 10) else "DOCS-2035"
    keywords = str((doc.metadata or {}).get("keywords") or "")
    parts = [part.strip() for part in keywords.split("|")]
    if len(parts) < 3:
        return False, "keywords-missing-third-token"
    if parts[2] != expected:
        return False, f"keywords-token:{parts[2] or 'empty'}!={expected}"
    return True, "ok"


def _ruble_gap_ok(page, profile: MethodProfile, expected_amount: str) -> Tuple[bool, str]:
    if profile.amount_ruble_gap is None:
        return True, "skip"
    want = _amount_digits(expected_amount)
    lo, hi = profile.amount_ruble_gap
    char_gaps: List[float] = []
    raw = page.get_text("rawdict") or {}
    for block in raw.get("blocks") or []:
        for line in block.get("lines") or []:
            chars = [
                char
                for span in line.get("spans") or []
                for char in span.get("chars") or []
            ]
            line_text = "".join(str(char.get("c") or "") for char in chars)
            if _amount_digits(line_text) != want:
                continue
            digit_indexes = [
                index for index, char in enumerate(chars)
                if str(char.get("c") or "").isdigit()
            ]
            if not digit_indexes:
                continue
            last_digit = digit_indexes[-1]
            currency = next(
                (
                    char for char in chars[last_digit + 1 :]
                    if str(char.get("c") or "") in ("i", "₽")
                ),
                None,
            )
            if currency is not None:
                char_gaps.append(
                    float(currency["bbox"][0]) - float(chars[last_digit]["bbox"][2])
                )
    if len(char_gaps) >= 2:
        for gap in char_gaps[:2]:
            if gap < lo or gap > hi:
                return False, f"visual-ruble-gap:{gap:.1f}pt"
        return True, "ok"

    words = list(page.get_text("words") or [])
    amount_words = [
        word for word in words
        if _amount_digits(str(word[4] or "")) == want
    ]
    currencies = [word for word in words if str(word[4] or "").strip() in ("i", "₽")]
    if len(amount_words) < 2 or not currencies:
        return False, "ruble-gap-missing-pair"
    checked = 0
    for amount_word in amount_words:
        candidates = [
            currency for currency in currencies
            if abs(float(currency[1]) - float(amount_word[1])) < 8.0
            and float(currency[0]) >= float(amount_word[2]) - 2.0
        ]
        if not candidates:
            continue
        gap = min(candidates, key=lambda item: item[0])[0] - amount_word[2]
        if gap < lo or gap > hi:
            return False, f"visual-ruble-gap:{gap:.1f}pt"
        checked += 1
    if checked < 2:
        return False, f"ruble-gap-count:{checked}/2"
    return True, "ok"


def strict_profile_integrity(
    pdf: bytes,
    *,
    expect: Optional[Dict[str, Any]],
    bank_hint: str,
) -> Tuple[bool, str]:
    """Fail-closed exact payload, profile, font and metadata validation."""
    import fitz

    profile = get_profile(bank_hint)
    if profile is None:
        return False, f"unknown-method-profile:{bank_hint or 'empty'}"
    if not isinstance(expect, dict) or not expect:
        return False, "missing-expect"
    lo, hi = effective_size_band(profile)
    if not (lo <= len(pdf) <= hi):
        return False, f"size-band:{len(pdf)} not {lo}-{hi}"
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception as exc:
        return False, f"profile-open:{type(exc).__name__}:{exc}"
    try:
        if doc.page_count != 1:
            return False, f"profile-pages:{doc.page_count}"
        page = doc[0]
        text = page.get_text() or ""
        normalized_text = _norm_space(text)
        for marker in profile.required_markers:
            if _norm_space(marker) not in normalized_text:
                return False, f"structure-marker:{marker}"
        if any(
            0xE000 <= ord(ch) <= 0xF8FF and ch not in profile.allow_visible_pua
            for ch in text
        ):
            return False, "visual-pua-onpage"
        if re.search(r"[\ufffd□☐�]", text):
            return False, "visual-tofu"

        expected_chars: Set[str] = set()
        for name, contract in profile.fields.items():
            ok, why = _field_exact(text, expect, name, contract)
            if not ok:
                return False, why
            value = _expect_value(expect, name)
            if _concrete(value):
                expected_chars.update(_masked_display(value, contract.display_mask))

        fok, fwhy = _font_sanity(doc, page, expected_chars)
        if not fok:
            return False, fwhy
        aok, awhy = _strict_anchor_check(page, profile, expect)
        if not aok:
            return False, awhy
        kok, kwhy = _keywords_ok(doc, profile, expect)
        if not kok:
            return False, kwhy
        gok, gwhy = _ruble_gap_ok(page, profile, _expect_value(expect, "amount"))
        if not gok:
            return False, gwhy
        return True, "ok"
    except Exception as exc:
        return False, f"profile-check-error:{type(exc).__name__}:{exc}"
    finally:
        doc.close()


def _span_lines(page) -> list:
    """[(text, x0, x1, y0, y1), ...] from span bboxes."""
    out = []
    for b in page.get_text("dict").get("blocks") or []:
        if b.get("type") not in (0, None) and b.get("type") != 0:
            continue
        for ln in b.get("lines") or []:
            spans = ln.get("spans") or []
            if not spans:
                continue
            t = "".join(str(s.get("text") or "") for s in spans)
            if not t.strip():
                continue
            x0 = min(float(s["bbox"][0]) for s in spans)
            x1 = max(float(s["bbox"][2]) for s in spans)
            y0 = min(float(s["bbox"][1]) for s in spans)
            y1 = max(float(s["bbox"][3]) for s in spans)
            out.append((_norm_space(t), x0, x1, y0, y1))
            # also each span alone (values often one span)
            for s in spans:
                st = str(s.get("text") or "")
                if not st.strip():
                    continue
                bb = s["bbox"]
                out.append((_norm_space(st), float(bb[0]), float(bb[2]), float(bb[1]), float(bb[3])))
    return out


def _find_span(lines, needle: str, *, min_len: int = 2):
    n = _norm_space(needle)
    if len(n) < min_len:
        return None
    # Prefer exact / longest match containing needle
    hits = [c for c in lines if n in c[0]]
    if not hits:
        return None
    hits.sort(key=lambda c: abs(len(c[0]) - len(n)))
    return hits[0]


def _layout_anchors_ok(
    page,
    *,
    expect: Dict[str, Any],
    bank_hint: str,
    text: str,
) -> Tuple[bool, str]:
    """Bank×method field-edge anchors + every expect field on face."""
    hint = (bank_hint or "").lower()
    lines = _span_lines(page)
    pw = float(page.rect.width)

    # --- lead-pad / clip / overflow on every span ---
    for t, x0, x1, _y0, _y1 in lines:
        if t[:2] == "  " and re.search(r"[А-Яа-яA-Za-z]", t):
            return False, f"visual-fio-lead-pad:{t.strip()[:24]}"
        if x0 < 1.5 and t.strip() and not t.strip().isdigit():
            return False, f"visual-left-clip:{t[:20]}"
        if x1 > pw + 1.5 and len(t.strip()) >= 2:
            return False, f"visual-right-overflow:{t[:20]}"

    sender = str(expect.get("sender") or expect.get("sender_name") or "").strip()
    recv = str(
        expect.get("receiver")
        or expect.get("receiver_name")
        or expect.get("recipient")
        or ""
    ).strip()
    phone = str(expect.get("phone") or expect.get("receiver_phone") or "").strip()
    bank = str(
        expect.get("bank")
        or expect.get("bank_name")
        or expect.get("recipient_bank")
        or ""
    ).strip()
    amt = str(expect.get("amount") or expect.get("new_amount") or "").strip()
    digs = re.sub(r"\D", "", amt)
    if len(digs) >= 5 and digs.endswith("00") and len(digs) > 5:
        digs_core = digs[:-2]
    else:
        digs_core = digs

    # Extra expect keys — must appear if user provided concrete value
    for key in (
        "commission",
        "account",
        "sender_account",
        "receiver_account",
        "operation_num",
        "sbp_id",
        "spb_number",
        "receipt_num",
        "message",
        "sender_card",
        "receiver_card",
    ):
        val = str(expect.get(key) or "").strip()
        if not val or val.lower() in ("авто", "auto", "-", "сейчас", "now"):
            continue
        # receipt / op often reformatted — require a distinctive chunk
        chunk = re.sub(r"\s+", "", val)
        if len(chunk) < 4:
            continue
        # cards: last 4
        if "card" in key:
            last4 = re.sub(r"\D", "", val)[-4:]
            if last4 and last4 not in re.sub(r"\D", "", text):
                return False, f"visual-missing-{key}:{last4}"
            continue
        if chunk[:6] not in re.sub(r"\s+", "", text) and val not in text:
            # phone-like / account masked
            dig = re.sub(r"\D", "", val)
            if dig and len(dig) >= 4 and dig[-4:] in re.sub(r"\D", "", text):
                continue
            return False, f"visual-missing-field:{key}"

    is_tbank = hint.startswith("tbank") or hint in ("tbank",)
    is_sber = hint.startswith("sber") or "сбер" in hint
    is_alfa = hint.startswith("alfa") or "альфа" in hint

    # ---- T-Bank: date LEFT x0=20; values RIGHT x1=250 ----
    if is_tbank:
        date_needle = ""
        parsed = _parse_expect_date(expect)
        dt_raw = _norm_space(
            str(expect.get("date_time") or expect.get("new_date") or expect.get("date") or "")
        )
        if re.search(r"\d{2}\.\d{2}\.\d{4}", dt_raw):
            date_needle = re.search(r"\d{2}\.\d{2}\.\d{4}", dt_raw).group(0)
        elif parsed:
            date_needle = f"{parsed[0]:02d}.{parsed[1]:02d}.{parsed[2]}"
        if date_needle:
            hit = _find_span(lines, date_needle)
            if not hit:
                return False, f"visual-missing-date-span:{date_needle}"
            if abs(hit[1] - 20.0) > 2.5:
                return False, f"visual-date-not-left:x0={hit[1]:.1f}"

        for needle, label in (
            (sender, "sender"),
            (recv, "receiver"),
            (phone, "phone"),
            (bank, "bank"),
        ):
            if not needle or len(needle) < 2:
                continue
            # skip alfa-style; tbank values right-anchored
            probe = needle if len(needle) <= 24 else needle[:16]
            hit = _find_span(lines, probe)
            if not hit and len(needle.split()) >= 1:
                hit = _find_span(lines, needle.split()[0])
            if not hit:
                continue  # text checks already cover missing
            # labels like «Отправитель» also x0=20 — skip short labels
            if hit[0] in ("Отправитель", "Получатель", "Телефон получателя", "Сумма", "Итого"):
                continue
            # value column right edge
            if abs(hit[2] - 250.0) > 3.5:
                # amount digits end before ₽ (~237–244) — allow if contains digits only
                if digs_core and digs_core in re.sub(r"\D", "", hit[0]):
                    if hit[2] < 230 or hit[2] > 252:
                        return False, f"visual-amt-edge:x1={hit[2]:.1f}"
                else:
                    return False, f"visual-{label}-not-right:x1={hit[2]:.1f}"

        if digs_core and len(digs_core) >= 3:
            # find amount-ish span containing digits
            amt_hits = [
                c for c in lines
                if digs_core in re.sub(r"\D", "", c[0]) and len(re.sub(r"\D", "", c[0])) >= 3
            ]
            if amt_hits:
                # prefer the one closest to right column
                amt_hits.sort(key=lambda c: abs(c[2] - 250.0))
                ax1 = amt_hits[0][2]
                if ax1 < 230 or ax1 > 252:
                    return False, f"visual-amt-not-right:x1={ax1:.1f}"

    # ---- Sber: values LEFT x0≈21; date center ~153 ----
    if is_sber:
        for needle, label in (
            (sender, "sender"),
            (recv, "receiver"),
            (phone, "phone"),
            (bank, "bank"),
        ):
            if not needle or len(needle) < 3:
                continue
            probe = needle.split()[0] if " " in needle else needle
            hit = _find_span(lines, probe[:12] if len(probe) > 12 else probe)
            if not hit:
                continue
            if hit[1] < 12 or hit[1] > 40:
                return False, f"visual-sber-{label}-not-left:x0={hit[1]:.1f}"

        if digs_core and len(digs_core) >= 3:
            amt_hits = [
                c for c in lines
                if digs_core in re.sub(r"\D", "", c[0])
            ]
            if amt_hits:
                if amt_hits[0][1] < 12 or amt_hits[0][1] > 45:
                    return False, f"visual-sber-amt-not-left:x0={amt_hits[0][1]:.1f}"

        # date center-ish (cx ≈ 153)
        parsed = _parse_expect_date(expect)
        if parsed:
            mon = _RU_MONTHS[parsed[1] - 1]
            day = parsed[0]
            date_hits = [
                c for c in lines
                if mon in c[0].lower() and str(parsed[2]) in c[0]
            ]
            if date_hits:
                cx = (date_hits[0][1] + date_hits[0][2]) / 2.0
                if abs(cx - 153.0) > 18.0:
                    return False, f"visual-sber-date-center:cx={cx:.1f}"

    # ---- Alfa: left col x0≈35.45; right col x0≈304.75 ----
    if is_alfa and not hint.startswith("alfa_statement"):
        left_needles = []
        right_needles = []
        if digs_core:
            left_needles.append((digs_core, "amount"))
        if recv:
            left_needles.append((recv.split()[0], "receiver"))
        if phone:
            right_needles.append((re.sub(r"\D", "", phone)[-4:], "phone"))
        if bank:
            right_needles.append((bank[:6], "bank"))
        for needle, label in left_needles:
            if not needle or len(needle) < 2:
                continue
            hit = _find_span(lines, needle) if not needle.isdigit() else None
            if needle.isdigit():
                hits = [c for c in lines if needle in re.sub(r"\D", "", c[0])]
                hit = hits[0] if hits else None
            if not hit:
                continue
            if abs(hit[1] - 35.45) > 8.0 and hit[1] < 200:
                # only flag if clearly in left half but wrong
                if hit[1] < 200 and (hit[1] < 25 or hit[1] > 55):
                    return False, f"visual-alfa-{label}-left:x0={hit[1]:.1f}"
        for needle, label in right_needles:
            if not needle or len(needle) < 2:
                continue
            if needle.isdigit():
                hits = [c for c in lines if needle in re.sub(r"\D", "", c[0])]
                hit = hits[0] if hits else None
            else:
                hit = _find_span(lines, needle)
            if not hit:
                continue
            if hit[1] > 200 and abs(hit[1] - 304.75) > 12.0:
                return False, f"visual-alfa-{label}-right:x0={hit[1]:.1f}"

    return True, "ok"


def visual_integrity(
    pdf: bytes,
    *,
    expect: Optional[Dict[str, Any]] = None,
    bank_hint: str = "",
    strict_fio: bool = True,
) -> Tuple[bool, str]:
    """Hard visual check — payload + coordinates for all banks."""
    import fitz

    expect = expect or {}
    hint = (bank_hint or "").lower()
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        page = doc[0]
        text = page.get_text() or ""
        words = page.get_text("words") or []
        blocks = page.get_text("blocks") or []
        # keep page for anchor pass
        page_ref = page
        doc_ref = doc
    except Exception as exc:
        return False, f"visual-open:{exc}"

    try:
        if len(blocks) < 3:
            return False, "visual-too-few-blocks"
        if _TOFU_RE.search(text):
            return False, "visual-tofu"

        if "Дамир" in text or "Марина Ч." in text:
            sender = str(expect.get("sender") or expect.get("sender_name") or "").strip()
            receiver = str(
                expect.get("receiver")
                or expect.get("receiver_name")
                or expect.get("recipient")
                or ""
            ).strip()
            # User-requested donor face names are OK; only reject leftover template FIO.
            if "Дамир" in text and "Дамир" not in sender and "Дамир" not in receiver:
                return False, "visual-donor-fio-leak"
            if "Марина Ч." in text and "Марина Ч." not in sender and "Марина Ч." not in receiver:
                return False, "visual-donor-fio-leak"

        skip_sender = hint.startswith("alfa_")
        skip_recv = hint.startswith("alfa_card")

        if hint.startswith("alfa_card"):
            compact_cards = re.sub(r"[\s\u00a0]", "", text)
            pans = re.findall(r"\d{6}\*{4,8}\d{4}", compact_cards)
            if len(pans) < 2:
                return False, f"visual-empty-cards:{len(pans)}"

        sender = str(expect.get("sender") or expect.get("sender_name") or "").strip()
        if sender and not skip_sender:
            parts = [p for p in sender.split() if len(p) >= 2]
            if parts and parts[0] not in text:
                return False, f"visual-missing-first:{parts[0]}"
            if len(parts) >= 2:
                last = parts[-1]
                if _INITIAL_ONLY_RE.match(last):
                    if last not in text and f"{last[0]}." not in text:
                        return False, f"visual-missing-last:{last}"
                elif len(last) > 3:
                    if last not in text:
                        if strict_fio:
                            if f"{last[0]}." in text and last not in text:
                                return False, f"visual-mangled-last:{last}"
                            if not _fio_token_ok(last, text, allow_shorten=False):
                                return False, f"visual-missing-last:{last}"
                        elif not _fio_token_ok(last, text, allow_shorten=True):
                            return False, f"visual-missing-last:{last}"

        recv = str(
            expect.get("receiver")
            or expect.get("receiver_name")
            or expect.get("recipient")
            or ""
        ).strip()
        if recv and not skip_recv:
            rparts = [p for p in recv.split() if p]
            r0 = rparts[0] if rparts else ""
            if len(r0) >= 3 and r0 not in text:
                if hint.startswith("alfa_phone") and r0[:3] in text and "**" in text:
                    pass
                else:
                    return False, f"visual-missing-recv:{r0}"
            if strict_fio and len(rparts) >= 2 and not hint.startswith("alfa_sbp"):
                rlast = rparts[-1]
                if len(rlast) > 3 and rlast not in text:
                    if f"{rlast[0]}." in text:
                        return False, f"visual-mangled-recv:{rlast}"
                    return False, f"visual-missing-recv-last:{rlast}"
            if hint.startswith("alfa_sbp"):
                if not re.search(
                    r"(?:ович|евич|овна|евна|ична)\s+[А-ЯЁ](?:\.|\s|$)",
                    text.replace("\xa0", " "),
                ):
                    return False, "visual-alfa-sbp-fio-shape"

        amt_raw = str(expect.get("amount") or expect.get("new_amount") or "").strip()
        digs = re.sub(r"\D", "", amt_raw)
        if len(digs) >= 5 and digs.endswith("00") and len(digs) > 5:
            core = digs[:-2]
            if not _amount_in_text(digs, text) and not _amount_in_text(core, text):
                return False, f"visual-missing-amt:{core or digs}"
        elif digs and len(digs) >= 3:
            if not _amount_in_text(digs, text):
                return False, f"visual-missing-amt:{digs}"

        dok, dwhy = _date_in_text(text, expect)
        if not dok:
            return False, dwhy

        # Empty glyf + ToUnicode still extracts the letter — catch near-zero
        # advance on face FIO/amount chars (blank holes / stacked overlap).
        face_chars = set()
        for key in ("sender", "sender_name", "receiver", "receiver_name"):
            for ch in str(expect.get(key) or ""):
                if ch.strip():
                    face_chars.add(ch)
        amt_d = re.sub(r"\D", "", str(expect.get("amount") or expect.get("new_amount") or ""))
        face_chars.update(amt_d)
        if face_chars:
            try:
                raw = page_ref.get_text("rawdict") or {}
                prev_x0 = None
                prev_y0 = None
                for block in raw.get("blocks") or []:
                    for line in block.get("lines") or []:
                        for span in line.get("spans") or []:
                            for chinfo in span.get("chars") or []:
                                c = chinfo.get("c") or ""
                                if c not in face_chars:
                                    prev_x0 = None
                                    continue
                                bb = chinfo.get("bbox") or (0, 0, 0, 0)
                                x0 = float(bb[0])
                                x1 = float(bb[2])
                                y0 = float(bb[1])
                                w = x1 - x0
                                if w < 0.4:
                                    return False, f"visual-empty-glyph:{c!r}"
                                # Stacked / overlapping advance (aw≈0 or bad Tm).
                                if (
                                    prev_x0 is not None
                                    and prev_y0 is not None
                                    and abs(y0 - prev_y0) < 1.2
                                    and abs(x0 - prev_x0) < 0.35
                                ):
                                    return False, f"visual-overlap-glyph:{c!r}"
                                prev_x0, prev_y0 = x0, y0
            except Exception:
                pass

        phone = str(expect.get("phone") or expect.get("receiver_phone") or "").strip()
        # Card / nocomm faces have no phone line — ignore payload phone if present.
        if phone and not any(x in hint for x in ("card", "nocomm")):
            if not _phone_in_text(text, phone):
                phone_tail = re.sub(r"\D", "", phone)[-4:]
                return False, f"visual-missing-phone:{phone_tail}"

        bank = str(
            expect.get("bank")
            or expect.get("bank_name")
            or expect.get("recipient_bank")
            or ""
        ).strip()
        # T-Bank phone face has no bank line.
        if bank and len(bank) >= 4 and "phone" not in hint:
            prefix = bank[:8] if len(bank) >= 8 else bank
            if prefix not in text and bank not in text:
                if bank.replace("-", "") not in text.replace("-", ""):
                    return False, f"visual-missing-bank:{prefix}"

        lok, lwhy = _layout_anchors_ok(
            page_ref, expect=expect, bank_hint=hint, text=text,
        )
        if not lok:
            return False, lwhy

        # T-Bank: PUA orphan markers / amount↔₽ hole (visible near stamp / sum).
        if hint.startswith("tbank") or hint in ("tbank",):
            if any(0xE000 <= ord(ch) < 0xF900 for ch in text):
                return False, "visual-pua-onpage"
            # Plain Latin junk near stamp (short PUA inject without Td).
            for w in words:
                t = (w[4] or "").strip()
                y0 = float(w[1])
                if y0 < 280 or y0 > 390:
                    continue
                if re.fullmatch(r"[A-Za-z]{1,4}", t) and t.lower() not in ("fb",):
                    return False, f"visual-stamp-junk:{t}"
            # ``strict_profile_integrity`` already checks both amount lines via
            # raw character geometry in ``_ruble_gap_ok``.  Do not repeat that
            # check with word boxes: MuPDF splits grouped amounts (``10 232``)
            # into ``10`` and ``232``, making the first fragment appear 31 pt
            # away from the ruble even though the final digit is only ~2 pt
            # away.
            words_list = list(words)

            # card_tbank / nocomm: Итого and Сумма must match (no commission line).
            if "card_tbank" in hint or "nocomm" in hint:
                face_amts = []
                for w in words_list:
                    t = (w[4] or "").strip()
                    if re.fullmatch(r"[\d\s]+", t) and re.search(r"\d", t) and len(re.sub(r"\D", "", t)) >= 3:
                        face_amts.append(re.sub(r"\D", "", t))
                # Unique multi-digit amounts on face (ignore receipt nums via length)
                uniq = []
                for a in face_amts:
                    if len(a) >= 3 and a not in uniq:
                        # skip receipt-like long runs
                        if len(a) >= 12:
                            continue
                        uniq.append(a)
                if len(uniq) >= 2 and uniq[0] != uniq[1]:
                    return False, f"visual-amt-mismatch:{uniq[0]}!={uniq[1]}"

        return True, "ok"
    finally:
        try:
            doc_ref.close()
        except Exception:
            pass


def emit_ok(
    pdf: Optional[bytes],
    *,
    bank_hint: str = "",
    expect: Optional[Dict[str, Any]] = None,
    strict_fio: bool = True,
    run_local_proton: bool = False,
) -> Tuple[bool, str]:
    """Pre-send / pre-accept gate. Payload, visuals and coordinates are HARD."""
    if not pdf:
        return False, "empty"
    pok, pwhy = strict_profile_integrity(
        pdf, expect=expect, bank_hint=bank_hint,
    )
    if not pok:
        return False, pwhy
    odd, total = count_odd_tj(pdf)
    if odd:
        return False, f"odd-tj:{odd}/{total}"
    vok, vwhy = visual_integrity(
        pdf,
        expect=expect,
        bank_hint=bank_hint,
        # Exact FIO/mask comparison already ran above.  Keep the legacy visual
        # pass for clipping/donor/stamp checks without reinterpreting masks.
        strict_fio=False,
    )
    if not vok:
        return False, vwhy
    if run_local_proton:
        verdict, detail = local_proton_verdict(pdf)
        if verdict != "ЧИСТО":
            return False, f"local-proton:{verdict}:{detail}"
    return True, "ok"


def gate_reason_user(why: str) -> str:
    """Short RU explanation for the bot user."""
    w = (why or "").lower()
    if "missing-amt" in w or "amt-not-right" in w or "amt-edge" in w or "ruble-gap" in w:
        return "сумма в PDF не совпала или съехала по координатам"
    if "missing-date" in w or "wrong-day" in w or "missing-year" in w or "date-not-left" in w or "date-center" in w:
        return "дата в PDF не совпала или съехала"
    if "missing-phone" in w or "phone-not-right" in w:
        return "телефон в PDF не совпал или съехал"
    if "missing-bank" in w or "bank-not" in w:
        return "банк получателя в PDF не совпал или съехал"
    if "not-right" in w or "not-left" in w or "left-clip" in w or "right-overflow" in w or "lead-pad" in w:
        return "поля съехали по координатам (левый/правый край)"
    if "mangled" in w or "missing-last" in w or "missing-first" in w or "missing-recv" in w:
        return "ФИО обрезалось или не попало в чек — сократите ФИО"
    if "missing-field" in w:
        return "одно из полей не попало в чек"
    if "donor-fio" in w:
        return "в чек попали чужие ФИО с шаблона"
    if "tofu" in w:
        return "битые символы в чеке"
    if "odd-tj" in w:
        return "структура PDF не собралась"
    if why == "empty":
        return "PDF пустой"
    return "проверка качества не прошла (данные или координаты)"
