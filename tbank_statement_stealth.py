"""T-Bank Statement (Справка о движении средств) stealth PDF generator.

Шаблон: templates/T_statement_original.pdf
Разблокированный: templates/T_statement_unlocked.pdf

Редактируемые поля:
  - main_date: дата вверху справки ("12.05.2026")
  - fio: ФИО ("Сеничев Дамир Евгеньевич")
  - address: адрес места жительства
  - period_from, period_to: даты периода
  - operations: список операций, каждая содержит:
      date_op, time_op, date_off, time_off, amount, description, card4
  - total_income: пополнения ("0,00")
  - total_outcome: расходы ("8 723,00")
"""

import os
import re
import zlib
import logging
import random
import string
from typing import Dict, List, Optional
from io import BytesIO

from fontTools.ttLib import TTFont

from tbank_unlock_template import (
    build_unlocked_template,
    FONT_REGULAR, FONT_MEDIUM,
)
from tbank_stealth_v3 import (
    _find_streams, _pad_to_compressed_size, _best_compress,
    _patch_pdf_metadata, _patch_length_and_rebuild, _ESC,
)

logger = logging.getLogger(__name__)
_DIR = os.path.dirname(os.path.abspath(__file__))
ORIG_TEMPLATE     = os.path.join(_DIR, "templates", "T_statement_original.pdf")
UNLOCKED_TEMPLATE = os.path.join(_DIR, "templates", "T_statement_unlocked.pdf")


# ------------------------------------------------------------------
# Подмножество глифов, под которое собран unlocked-шаблон
# ------------------------------------------------------------------
def _stmt_unicodes_reg() -> set:
    s = set()
    s.update(ord(c) for c in "0123456789")
    s.update(ord(c) for c in " .,:;()[]{}*-+/№<>=\"'?!@#$%&|~`^_\\")
    s.update(range(0x0410, 0x0450))
    s.update([0x0401, 0x0451])
    s.update(range(ord("A"), ord("Z") + 1))
    s.update(range(ord("a"), ord("z") + 1))
    s.update([0x00A0, 0x20BD, 0x2116, 0x2009, 0x202F])
    s.update([0x00AB, 0x00BB])
    return s


def _stmt_unicodes_med() -> set:
    s = set()
    s.update(ord(c) for c in "0123456789 .,:;()[]{}*-+/№")
    s.update(range(0x0410, 0x0450))
    s.update([0x0401, 0x0451])
    s.update([0x00A0, 0x20BD, 0x2116, 0x2009, 0x202F])
    return s


# ------------------------------------------------------------------
# Шрифты, CID-карты
# ------------------------------------------------------------------
_F_REG: Optional[TTFont] = None
_F_MED: Optional[TTFont] = None
_C2G_REG: Dict[str, int] = {}
_C2G_MED: Dict[str, int] = {}
_G2C_REG: Dict[int, str] = {}
_G2C_MED: Dict[int, str] = {}


def _load_fonts():
    global _F_REG, _F_MED, _C2G_REG, _C2G_MED, _G2C_REG, _G2C_MED
    if _F_REG and _F_MED:
        return
    _F_REG = TTFont(FONT_REGULAR)
    _F_MED = TTFont(FONT_MEDIUM)
    for c2g, g2c, f in ((_C2G_REG, _G2C_REG, _F_REG), (_C2G_MED, _G2C_MED, _F_MED)):
        cmap = f.getBestCmap() or {}
        for cp, name in cmap.items():
            try:
                gid = f.getGlyphID(name)
                c2g[chr(cp)] = gid
                if gid not in g2c:
                    g2c[gid] = chr(cp)
            except Exception:
                pass
    logger.info(f"STMT fonts loaded: reg={len(_C2G_REG)} med={len(_C2G_MED)}")


def _enc(text: str, medium: bool = False) -> bytes:
    """Encode text into UTF16-style GID byte sequence + escape () \\."""
    _load_fonts()
    m = _C2G_MED if medium else _C2G_REG
    raw = bytearray()
    for ch in text:
        gid = m.get(ch, m.get('?', 0))
        raw += gid.to_bytes(2, 'big')
    # Escape () and \ for PDF literal string
    out = bytearray()
    for b in raw:
        if b in _ESC:
            out += _ESC[b]
        else:
            out.append(b)
    return bytes(out)


# ------------------------------------------------------------------
# Утилиты для работы с content stream
# ------------------------------------------------------------------
def _ensure_unlocked() -> None:
    if os.path.exists(UNLOCKED_TEMPLATE):
        return
    if not os.path.exists(ORIG_TEMPLATE):
        raise RuntimeError(f"Original template not found: {ORIG_TEMPLATE}")
    logger.info("Building T_statement_unlocked.pdf ...")
    build_unlocked_template(
        orig_path=ORIG_TEMPLATE,
        out_path=UNLOCKED_TEMPLATE,
        unicodes_reg=_stmt_unicodes_reg(),
        unicodes_med=_stmt_unicodes_med(),
    )


def _find_main_stream(pdf: bytes):
    """Find the content stream that contains a known text marker."""
    marker = _enc("Справка")  # all subset → encoded
    # 'Справка' likely in medium font (header); try both
    marker_med = _enc("Справка", medium=True)
    for cs, ce, raw in _find_streams(pdf):
        if not raw[:2].startswith(b"\x78"):
            continue
        try:
            dec = zlib.decompress(raw)
        except Exception:
            continue
        if marker in dec or marker_med in dec:
            return cs, ce, raw, dec
    return None


def _replace_in_tj(stream: bytes, anchor_enc: bytes, new_block_enc: bytes) -> tuple[bytes, bool]:
    """Find anchor inside a (...)Tj or (...)Tj-like block, replace ENTIRE
    parenthesised content with new_block_enc.

    Walks back to '(' before anchor, forward to ')' after, then substitutes.
    """
    idx = stream.find(anchor_enc)
    if idx < 0:
        return stream, False
    # Walk back to '(' — must skip escaped \( inside
    start = idx
    while start > 0 and stream[start - 1] != 0x28:  # '('
        start -= 1
    if start == 0:
        return stream, False
    # Walk forward to unescaped ')'
    end = idx + len(anchor_enc)
    while end < len(stream) and stream[end] != 0x29:  # ')'
        end += 1
    if end >= len(stream):
        return stream, False
    new = stream[:start] + new_block_enc + stream[end:]
    return new, True


def _replace_bytes(stream: bytes, old: bytes, new: bytes, count: int = 1) -> tuple[bytes, int]:
    """Replace first N occurrences of `old` with `new` (raw bytes)."""
    if not old or old == new:
        return stream, count
    out = bytearray()
    pos = 0
    replaced = 0
    while replaced < count:
        idx = stream.find(old, pos)
        if idx < 0:
            break
        out += stream[pos:idx]
        out += new
        pos = idx + len(old)
        replaced += 1
    out += stream[pos:]
    return bytes(out), replaced


def _replace_tj_block_by_anchor(stream: bytes, anchor_text: str,
                                  new_text: str, enc_fn) -> tuple[bytes, bool]:
    anchor_enc = enc_fn(anchor_text)
    new_enc = enc_fn(new_text)
    return _replace_in_tj(stream, anchor_enc, new_enc)


# ------------------------------------------------------------------
# Metadata randomization (как в tbank_nocomm_stealth)
# ------------------------------------------------------------------
def _randomize_keywords(pdf: bytes) -> bytes:
    def _rh(n): return "".join(random.choices(string.hexdigits[:16], k=n))
    def _uuid(): return f"{_rh(8)}-{_rh(4)}-{_rh(4)}-{_rh(4)}-{_rh(12)}"
    new_kw = f"{_uuid()} | DOCS-{random.randint(100, 999)}"
    pdf = re.sub(
        rb"(/Keywords\s*\([^|]+\|\s*)[0-9a-fA-F]{32}(\s*\|\s*\d+\s*\))",
        lambda m: m.group(1) + new_kw.encode("latin1") + b")",
        pdf,
    )
    return pdf


def _randomize_subject_id(pdf: bytes) -> bytes:
    """Title strings 'Исх. № c1fb36aa' have an ID; ID is also in Keywords UUID prefix.
    We let Keywords randomize separately. The visible 'Исх. №' inline is in content
    stream and we replace it there via _enc.
    """
    return pdf


# ------------------------------------------------------------------
# Подготовка пользовательских данных
# ------------------------------------------------------------------
_ORIG = {
    "main_date":     "12.05.2026",
    "fio":           "Сеничев Дамир Евгеньевич",
    "addr_zip":      "423826",
    "addr_full":     "423826, Респ Татарстан, Г Набережные Челны, Ул Шамиля Усманова , д. 129, кв. 275",
    "period_from":   "12.05.2026",
    "period_to":     "12.05.2026",
    "total_income":  "0,00",
    "total_outcome": "8 723,00",
    "ops": [
        {"date_op": "12.05.2026", "time_op": "12:26",
         "date_off": "12.05.2026", "time_off": "12:42",
         "amount": "-974.00 ",
         "desc1": "Оплата в YANDEX*EDA", "desc2": "MOSCOW RU",
         "card4": "1666"},
        {"date_op": "12.05.2026", "time_op": "00:58",
         "date_off": "12.05.2026", "time_off": "04:15",
         "amount": "-7 749.00 ",
         "desc1": "Внутренний перевод на", "desc2": "договор 8147507381",
         "card4": "1666"},
    ],
}


def _fmt_amount(s: str) -> str:
    """Принимает '974' / '974.00' / '-7 749' и возвращает '-7 749.00 '."""
    s = str(s).strip()
    sign = "-" if s.startswith("-") else ""
    if sign: s = s[1:].strip()
    # Парсим число
    s = s.replace(",", ".").replace("\u00A0", " ").replace("\u2009", " ")
    m = re.match(r"^([\d\s]+)(?:\.(\d{1,2}))?$", s)
    if not m:
        return f"-{s} "
    int_part = re.sub(r"\s", "", m.group(1))
    dec = m.group(2) or "00"
    if len(dec) == 1: dec += "0"
    # форматируем тысячи с пробелом
    int_fmt = f"{int(int_part):,}".replace(",", " ")
    return f"-{int_fmt}.{dec} " if sign or True else f"{int_fmt}.{dec} "


def _fmt_total(s: str) -> str:
    """Формат '8 723,00'."""
    s = str(s).strip().replace(",", ".").replace("\u00A0", " ").replace("\u2009", " ")
    m = re.match(r"^([\d\s]+)(?:\.(\d{1,2}))?$", s)
    if not m:
        return s
    int_part = re.sub(r"\s", "", m.group(1))
    dec = m.group(2) or "00"
    if len(dec) == 1: dec += "0"
    int_fmt = f"{int(int_part):,}".replace(",", " ")
    return f"{int_fmt},{dec}"


# ------------------------------------------------------------------
# Главная функция генерации
# ------------------------------------------------------------------
def _apply_statement_replacements(stream: bytes, p: Dict, ops: List[Dict],
                                  er, em) -> tuple[bytes, dict]:
    ok = {}

    if p["fio"] != _ORIG["fio"]:
        stream, ok["fio"] = _replace_tj_block_by_anchor(
            stream, "Сеничев", p["fio"], lambda t: em(t))
    else:
        ok["fio"] = True

    if p["address"] != _ORIG["addr_full"]:
        anchor = er("423826")
        idx = stream.find(anchor)
        if idx >= 0:
            paren_open = stream.rfind(b'(', max(0, idx - 50), idx)
            paren_close_rel = stream[idx:idx + 1500].find(b')Tj')
            paren_close = idx + paren_close_rel if paren_close_rel >= 0 else -1
            if paren_open >= 0 and paren_close > 0:
                new_addr_enc = b"(" + er(" " + p["address"]) + b")Tj"
                stream = stream[:paren_open] + new_addr_enc + stream[paren_close + 3:]
                ok["address"] = True
            else:
                ok["address"] = False
        else:
            ok["address"] = False
    else:
        ok["address"] = True

    new_period = f"{p['period_from']} по {p['period_to']}"
    old_period = f"{_ORIG['period_from']} по {_ORIG['period_to']}"
    if new_period != old_period:
        stream, n = _replace_bytes(stream, er(old_period), er(new_period), count=1)
        ok["period"] = n == 1
    else:
        ok["period"] = True

    if p["main_date"] != _ORIG["main_date"]:
        stream, n = _replace_bytes(stream, er(_ORIG["main_date"]), er(p["main_date"]), count=1)
        ok["main_date"] = n == 1
    else:
        ok["main_date"] = True

    cursor = 0

    def replace_next(old: str, new: str, medium=False) -> bool:
        nonlocal stream, cursor
        enc_fn = em if medium else er
        old_e = enc_fn(old)
        new_e = enc_fn(new)
        idx = stream.find(old_e, cursor)
        if idx < 0:
            return False
        stream = stream[:idx] + new_e + stream[idx + len(old_e):]
        cursor = idx + len(new_e)
        return True

    hdr = er("Дата и время")
    hidx = stream.find(hdr)
    cursor = hidx if hidx >= 0 else 0

    for i, op in enumerate(ops):
        orig = _ORIG["ops"][i]
        ok[f"op{i}.date_op"]  = replace_next(orig["date_op"],  op["date_op"])
        ok[f"op{i}.time_op"]  = replace_next(orig["time_op"],  op["time_op"])
        ok[f"op{i}.date_off"] = replace_next(orig["date_off"], op["date_off"])
        ok[f"op{i}.time_off"] = replace_next(orig["time_off"], op["time_off"])
        ok[f"op{i}.amount1"]  = replace_next(orig["amount"],   op["amount"])
        ok[f"op{i}.amount2"]  = replace_next(orig["amount"],   op["amount"])
        ok[f"op{i}.desc1"]    = replace_next(orig["desc1"],    op["desc1"])
        ok[f"op{i}.desc2"]    = replace_next(orig["desc2"],    op["desc2"])
        ok[f"op{i}.card4"]    = replace_next(orig["card4"],    op["card4"])

    cursor = 0
    ok["total_outcome"] = replace_next(_ORIG["total_outcome"], p["total_outcome"])
    cursor = 0
    ok["total_income"]  = replace_next(_ORIG["total_income"],  p["total_income"])

    return stream, ok


def _prepare_statement_data(data: Dict) -> Dict:
    p = {
        "main_date":     str(data.get("main_date",     _ORIG["main_date"])).strip(),
        "fio":           str(data.get("fio",           _ORIG["fio"])).strip(),
        "address":       str(data.get("address",       _ORIG["addr_full"])).strip(),
        "period_from":   str(data.get("period_from",   _ORIG["period_from"])).strip(),
        "period_to":     str(data.get("period_to",     _ORIG["period_to"])).strip(),
        "total_income":  _fmt_total(data.get("total_income",  _ORIG["total_income"])),
        "total_outcome": _fmt_total(data.get("total_outcome", _ORIG["total_outcome"])),
    }
    ops_in: List[Dict] = data.get("operations") or _ORIG["ops"]
    ops_in = list(ops_in)[:2]
    while len(ops_in) < 2:
        ops_in.append(_ORIG["ops"][len(ops_in)])

    ops = []
    for i, op in enumerate(ops_in):
        orig = _ORIG["ops"][i]
        ops.append({
            "date_op":  str(op.get("date_op",  orig["date_op"])).strip(),
            "time_op":  str(op.get("time_op",  orig["time_op"])).strip(),
            "date_off": str(op.get("date_off", op.get("date_op", orig["date_off"]))).strip(),
            "time_off": str(op.get("time_off", op.get("time_op", orig["time_off"]))).strip(),
            "amount":   _fmt_amount(op.get("amount", orig["amount"])),
            "desc1":    str(op.get("desc1", orig["desc1"])).strip(),
            "desc2":    str(op.get("desc2", orig["desc2"])).strip(),
            "card4":    str(op.get("card4", orig["card4"])).strip()[-4:],
        })
    p["ops"] = ops
    return p


def _try_donor_orig_statement(prepared: Dict) -> Optional[bytes]:
    """Donor-orig для выписки: пока нет in-place — сразу dynamic."""
    return None


def _build_dynamic_statement(prepared: Dict) -> Optional[bytes]:
    from tbank_dynamic import build_dynamic_tbank
    from tbank_channel_common import patch_channel_metadata

    p = prepared
    ops = p["ops"]
    need_r = [
        p["main_date"], p["address"], p["period_from"], p["period_to"],
        p["total_income"], p["total_outcome"],
        f"{_ORIG['period_from']} по {_ORIG['period_to']}",
        f"{p['period_from']} по {p['period_to']}",
    ]
    need_m = [p["fio"]]
    for op in ops:
        need_r.extend([
            op["date_op"], op["time_op"], op["date_off"], op["time_off"],
            op["amount"], op["desc1"], op["desc2"], op["card4"],
        ])
        for orig in _ORIG["ops"]:
            need_r.extend([
                orig["date_op"], orig["time_op"], orig["date_off"], orig["time_off"],
                orig["amount"], orig["desc1"], orig["desc2"], orig["card4"],
            ])

    def apply(stream, er, em, rtj, replace_once, _er_soft=None):
        new_stream, ok = _apply_statement_replacements(stream, p, ops, er, em)
        fails = [k for k, v in ok.items() if not v]
        if fails:
            logger.warning(f"Stmt fields failed: {fails}")
        return new_stream, ok

    return build_dynamic_tbank(
        orig_path=ORIG_TEMPLATE,
        need_r_texts=need_r,
        need_m_texts=need_m,
        apply_replacements=apply,
        metadata_date=p["main_date"],
        patch_metadata_fn=patch_channel_metadata,
        log_label="🟡 STATEMENT DYNAMIC",
        best_compress=_best_compress,
        need_m_itogo=False,
    )


def create_tbank_statement(data: Dict) -> Optional[bytes]:
    """Сгенерировать выписку — donor-orig → dynamic (как SBP)."""
    try:
        from tbank_channel_common import create_channel_stealth

        prepared = _prepare_statement_data(data)
        return create_channel_stealth(
            prepared,
            _try_donor_orig_statement,
            _build_dynamic_statement,
            channel="statement",
        )
    except Exception as e:
        logger.error(f"create_tbank_statement error: {e}", exc_info=True)
        return None


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    sample = {
        "main_date":     "08.05.2026",
        "fio":           "Иванов Иван Иванович",
        "address":       "117303, г Москва, Севастопольский проспект, д. 28, кв. 12",
        "period_from":   "01.05.2026",
        "period_to":     "08.05.2026",
        "total_income":  "12500",
        "total_outcome": "35780",
        "operations": [
            {"date_op": "05.05.2026", "time_op": "10:15",
             "date_off": "05.05.2026", "time_off": "14:30",
             "amount": "12500",
             "desc1": "Оплата в OZON.RU",
             "desc2": "MOSCOW RU",
             "card4": "1234"},
            {"date_op": "07.05.2026", "time_op": "19:45",
             "date_off": "07.05.2026", "time_off": "20:01",
             "amount": "23280",
             "desc1": "Перевод по СБП",
             "desc2": "Сбербанк",
             "card4": "1234"},
        ],
    }
    res = create_tbank_statement(sample)
    if res:
        out = os.path.join(_DIR, "_test_statement.pdf")
        with open(out, "wb") as f:
            f.write(res)
        print(f"saved: {out} ({len(res)} bytes)")
    else:
        print("FAILED")
