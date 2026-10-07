# -*- coding: utf-8 -*-
"""Эмиттер чека Сбербанка «Перевод по СБП» по схеме настоящего банковского PDF.

Принцип: ничего не «патчим» в чужом документе, а собираем файл ровно так, как его
собирает банк (JasperReports 6.18.1 -> iText 2.1.7):

* все постоянные объекты (картинки, страница, каталог) берутся из шаблона побайтно;
* content stream — тот же набор блоков BT/Tm/Tf/Tj, значения в CID-литералах;
* шрифт F1 = ArialMT (3419 глифов), subset: непустые глифы = использованные CID
  + компоненты композитов + .notdef; таблицы cvt/fpgm/prep/hmtx/maxp/hhea/head — константы;
  глифы берутся из настоящих банковских шрифтов (одинаковые байты на один GID);
* /W = floor(advance*1000/2048), ToUnicode — по одной записи на CID, как у iText;
* все Flate-потоки — zlib level 6 (Java Deflater default), xref/trailer как у iText.

Никаких ограничений на текст: любые символы, любая длина, никакой подмены/расширения.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import math
import os
import random
import re
import struct
import zlib
from typing import Dict, List, Optional, Tuple

_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_DIR, "sber_faithful_data")
_NG = 3419
_RUB_CID = 3418
_SPACE_CID = 3
_NOTDEF_CID = 0

_cache: Dict[str, object] = {}

# значения полей (Y базовой линии) в странице 300x795
_Y = {
    "date": "711.74",
    "recipient": "604.74",
    "phone": "565.74",
    "bank": "524.74",
    "sender": "475.74",
    "account": "434.74",
    "amount": "385.74",
    "fee": "345.74",
    "opid": "304.74",
}
_DATE_CENTER = 153.0
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря")

# «По номеру телефона», страница 300×699. Дата без ведущего нуля, суммы с одним пробелом перед ₽.
_PHONE_Y = {
    "date": "615.74",
    "recipient": "524.74",
    "phone": "490.74",
    "recv_label": "474.12",
    "recv_account": "456.74",
    "sender": "398.74",
    "account": "364.74",
    "amount": "330.74",
    "fee": "296.74",
    "doc": "238.74",
    "auth": "204.74",
}


# ───────────────────────────── данные ─────────────────────────────
def _load() -> Dict[str, object]:
    if _cache:
        return _cache
    with open(os.path.join(_DATA, "arial_base.json"), encoding="utf-8") as fp:
        base = json.load(fp)
    _cache["tables"] = {k: base64.b64decode(v) for k, v in base["tables"].items()}
    _cache["uni2gid"] = {int(k): v for k, v in base["uni2gid"].items()}
    with open(os.path.join(_DATA, "arial_glyphs.json"), encoding="utf-8") as fp:
        gl = json.load(fp)
    _cache["genuine"] = {int(k): bytes.fromhex(v) for k, v in gl.items()}
    synth: Dict[int, bytes] = {}
    sp = os.path.join(_DATA, "arial_glyphs_synth.json")
    if os.path.isfile(sp):
        with open(sp, encoding="utf-8") as fp:
            synth = {int(k): bytes.fromhex(v) for k, v in json.load(fp).items()}
    _cache["synth"] = synth
    merged = dict(synth)
    merged.update(_cache["genuine"])  # type: ignore[arg-type]
    _cache["glyphs"] = merged
    with open(os.path.join(_DATA, "sbp_template.pdf"), "rb") as fp:
        _cache["template"] = fp.read()
    return _cache


def _phone_template() -> bytes:
    d = _load()
    if "phone_template" not in d:
        with open(os.path.join(_DATA, "phone_template.pdf"), "rb") as fp:
            d["phone_template"] = fp.read()
    return d["phone_template"]  # type: ignore[return-value]


def _objects(pdf: bytes) -> Dict[int, bytes]:
    objs: Dict[int, bytes] = {}
    for m in re.finditer(rb"(?<![0-9])(\d+) 0 obj\b", pdf):
        s = m.start()
        e = pdf.find(b"endobj", s) + 6
        if e > 5:
            objs[int(m.group(1))] = pdf[s:e]
    # stream-объекты содержат бинарные данные: уточняем границы по endstream
    return objs


def _split_stream(obj: bytes) -> Tuple[bytes, bytes, bytes]:
    m = re.search(rb"stream\r?\n", obj)
    end = obj.rfind(b"\nendstream")
    return obj[: m.end()], obj[m.end():end], obj[end:]


# ───────────────────────────── кодирование текста ─────────────────────────────
def char_to_cid(ch: str) -> int:
    d = _load()
    if ch == "\u20bd":
        return _RUB_CID
    if ch in ("\u00a0", "\u202f", "\u2009"):
        ch = " "
    return d["uni2gid"].get(ord(ch), _NOTDEF_CID)  # type: ignore[union-attr]


def text_to_cids(text: str) -> List[int]:
    return [char_to_cid(c) for c in text]


def _escape_literal(raw: bytes) -> bytes:
    out = bytearray()
    for c in raw:
        if c == 0x5C:
            out += b"\\\\"
        elif c == 0x28:
            out += b"\\("
        elif c == 0x29:
            out += b"\\)"
        elif c == 0x0D:
            out += b"\\r"
        elif c == 0x0A:
            out += b"\\n"
        elif c == 0x09:
            out += b"\\t"
        elif c == 0x08:
            out += b"\\b"
        elif c == 0x0C:
            out += b"\\f"
        else:
            out.append(c)
    return bytes(out)


def _unescape_literal(s: bytes) -> bytes:
    out = bytearray()
    i = 0
    mp = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
    while i < len(s):
        c = s[i]
        if c == 0x5C and i + 1 < len(s):
            n = s[i + 1]
            if n in mp:
                out.append(mp[n]); i += 2; continue
            if 0x30 <= n <= 0x37:
                j = i + 1; v = 0; k = 0
                while j < len(s) and k < 3 and 0x30 <= s[j] <= 0x37:
                    v = v * 8 + s[j] - 0x30; j += 1; k += 1
                out.append(v & 255); i = j; continue
            out.append(n); i += 2; continue
        out.append(c); i += 1
    return bytes(out)


def cids_to_literal(cids: List[int]) -> bytes:
    raw = b"".join(struct.pack(">H", c) for c in cids)
    return _escape_literal(raw)


def _num(v: float) -> str:
    s = ("%.2f" % v).rstrip("0").rstrip(".")
    return s or "0"


def advance_floor(cid: int) -> int:
    hm = _load()["tables"]["hmtx"]  # type: ignore[index]
    nhm = struct.unpack(">H", _load()["tables"]["hhea"][34:36])[0]  # type: ignore[index]
    i = min(cid, nhm - 1)  # глифы за numberOfHMetrics используют последний advance
    adv = struct.unpack(">H", hm[i * 4:i * 4 + 2])[0]
    return (adv * 1000) // 2048


def text_width_pt(text: str, size: float) -> float:
    return sum(advance_floor(c) for c in text_to_cids(text)) * size / 1000.0


# ───────────────────────────── content ─────────────────────────────
def _find_tj_blocks(cs: bytes) -> List[Tuple[int, int, int, int, str]]:
    """[(start_of_Tm_x, end_of_x, str_start, str_end, y_text)] для каждого BT-блока с Tj."""
    res = []
    pat = re.compile(rb"1 0 0 1 ([\d.\-]+) ([\d.\-]+) Tm\s+/F1 \d+ Tf\s+[\d. ]+ rg\s+\(")
    for m in pat.finditer(cs):
        i = m.end()
        while i < len(cs):
            c = cs[i]
            if c == 0x5C:
                i += 2
                continue
            if c == 0x29:
                break
            i += 1
        res.append((m.start(1), m.end(1), m.end(), i, m.group(2).decode()))
    return res


def build_content(
    template_cs: bytes,
    values: Dict[str, str],
    date_center_text: str,
    ymap: Optional[Dict[str, str]] = None,
    date_center: float = _DATE_CENTER,
) -> bytes:
    ymap = ymap or _Y
    blocks = _find_tj_blocks(template_cs)
    by_y = {b[4]: b for b in blocks}
    edits = []  # (start, end, bytes)
    for key, ytxt in ymap.items():
        b = by_y.get(ytxt)
        if b is None:
            raise ValueError("template block not found: %s" % key)
        txt = values[key]
        edits.append((b[2], b[3], cids_to_literal(text_to_cids(txt))))
        if key == "date":
            w = text_width_pt(txt, 12.0)
            edits.append((b[0], b[1], _num(round(date_center - w / 2.0, 2)).encode()))
    out = bytearray(template_cs)
    for s, e, new in sorted(edits, key=lambda x: -x[0]):
        out[s:e] = new
    return bytes(out)


# ───────────────────────────── шрифт ─────────────────────────────
def _glyph_components(raw: bytes) -> List[int]:
    if len(raw) < 10:
        return []
    nc = struct.unpack(">h", raw[:2])[0]
    if nc >= 0:
        return []
    i = 10
    res = []
    while i + 4 <= len(raw):
        fl, gi = struct.unpack(">HH", raw[i:i + 4])
        res.append(gi)
        i += 4
        i += 4 if fl & 1 else 2
        if fl & 8:
            i += 2
        elif fl & 0x40:
            i += 4
        elif fl & 0x80:
            i += 8
        if not fl & 0x20:
            break
    return res


def _cks(d: bytes) -> int:
    d = d + b"\0" * (-len(d) % 4)
    return sum(struct.unpack(">%dI" % (len(d) // 4), d)) & 0xFFFFFFFF


def glyph_bytes(gid: int) -> Optional[bytes]:
    return _load()["glyphs"].get(gid)  # type: ignore[union-attr]


def missing_glyphs(cids) -> List[int]:
    """CID без какого-либо глифа (ни настоящего, ни приближённого)."""
    g = _load()["glyphs"]
    return sorted(c for c in set(cids) if c not in (_SPACE_CID,) and c not in g)  # type: ignore[operator]


def approx_glyphs(cids) -> List[int]:
    """CID, чьи глифы синтезированы (контур точный, хинт-программа приближённая)."""
    gen = _load()["genuine"]
    return sorted(c for c in set(cids) if c != _SPACE_CID and c not in gen)  # type: ignore[operator]


def build_font(used: List[int]) -> bytes:
    d = _load()
    glyphs: Dict[int, bytes] = d["glyphs"]  # type: ignore[assignment]
    tabs: Dict[str, bytes] = dict(d["tables"])  # type: ignore[arg-type]
    need = set(used) | {_NOTDEF_CID}
    need.discard(_SPACE_CID)
    todo = list(need)
    while todo:
        g = todo.pop()
        raw = glyphs.get(g)
        if raw is None:
            continue
        for c in _glyph_components(raw):
            if c not in need:
                need.add(c)
                todo.append(c)
    glyf = bytearray()
    loca = []
    for g in range(_NG):
        loca.append(len(glyf))
        if g in need and g in glyphs:
            glyf += glyphs[g]
    loca.append(len(glyf))
    tabs["glyf"] = bytes(glyf)
    tabs["loca"] = struct.pack(">%dI" % (_NG + 1), *loca)
    tags = sorted(tabs)
    n = len(tags)
    off = 12 + 16 * n
    sr = 1
    while sr * 2 <= n:
        sr *= 2
    hdr = struct.pack(">IHHHH", 0x00010000, n, sr * 16, int(math.log2(sr)), n * 16 - sr * 16)
    dirs = b""
    body = b""
    for t in tags:
        data = tabs[t]
        cdata = data[:8] + b"\0\0\0\0" + data[12:] if t == "head" else data  # как у iText
        dirs += struct.pack(">4sIII", t.encode("latin-1"), _cks(cdata), off + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    return hdr + dirs + body


def build_w_array(used: List[int]) -> bytes:
    cids = sorted(set(used))
    parts = []
    i = 0
    while i < len(cids):
        j = i
        while j + 1 < len(cids) and cids[j + 1] == cids[j] + 1:
            j += 1
        ws = " ".join(str(advance_floor(c)) for c in cids[i:j + 1])
        parts.append("%d[%s]" % (cids[i], ws))
        i = j + 1
    return ("[" + "".join(parts) + "]").encode()


def _gid_uni_map() -> Dict[int, int]:
    d = _load()
    if "gid2uni" not in d:
        m: Dict[int, int] = {}
        for u, g in sorted(d["uni2gid"].items(), reverse=True):  # type: ignore[union-attr]
            m[g] = u
        m[_RUB_CID] = 0x20BD
        m[_SPACE_CID] = 0x20
        d["gid2uni"] = m
    return d["gid2uni"]  # type: ignore[return-value]


def build_tounicode(used: List[int], uni_by_cid: Dict[int, int]) -> bytes:
    cids = sorted(set(used))
    head = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CIDSystemInfo\n"
        "<< /Registry (TTX+0)\n/Ordering (T42UV)\n/Supplement 0\n>> def\n/CMapName /TTX+0 def\n"
        "/CMapType 2 def\n1 begincodespacerange\n<0000><FFFF>\nendcodespacerange\n"
    )
    tail = "CMapName currentdict /CMap defineresource pop\nend end\n"
    body = ""
    for k in range(0, len(cids), 100):
        chunk = cids[k:k + 100]
        body += "%d beginbfrange\n" % len(chunk)
        for c in chunk:
            u = uni_by_cid.get(c, 0xFFFD)
            body += "<%04x><%04x><%04x>\n" % (c, c, u)
        body += "endbfrange\n"
    return (head + body + "endcmap\n" + tail).encode("latin-1")


# ───────────────────────────── даты / ID ─────────────────────────────
def format_face_date(when: _dt.datetime) -> str:
    return "%02d %s %d %02d:%02d:%02d (МСК)" % (
        when.day, _MONTHS[when.month - 1], when.year, when.hour, when.minute, when.second)


def format_phone_date(when: _dt.datetime) -> str:
    """Как на чеке «по номеру телефона»: день без ведущего нуля."""
    return "%d %s %d %02d:%02d:%02d (МСК)" % (
        when.day, _MONTHS[when.month - 1], when.year, when.hour, when.minute, when.second)


def _rand_tag() -> str:
    return "".join(random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6))


def _rand_file_id() -> bytes:
    return os.urandom(16).hex().encode()


def generate_op_id(when_msk: _dt.datetime, *, ref4_pool: Optional[tuple] = None) -> str:
    """ID операции СБП (32 симв.) по структуре настоящих чеков.

    A|B + YDDD + HHMMSS(UTC, на 3-9 с раньше операции) + ref4 + (цифра|буква) + '0' + (G|B)
    + 4-значный маршрут + '0011' + NN + 4 цифры.

    Две зависимости от даты, выведенные по 13 оригиналам (21.12.2025 – 21.06.2026):
    * маркер: «B» до 21.03.2026 включительно, «A» с 01.04.2026;
    * счётчик NN растёт линейно ≈ 66 + 0.0686·дней от 21.12.2025 (66 → 79 за полгода).
    """
    core = when_msk - _dt.timedelta(hours=3, seconds=random.randint(3, 9))
    doy = core.timetuple().tm_yday
    ydoy = (core.year - 2020) * 1000 + doy
    marker = "B" if when_msk < _dt.datetime(2026, 3, 25) else "A"
    days = (when_msk - _dt.datetime(2025, 12, 21)).total_seconds() / 86400.0
    nn = int(round(66 + 0.0686 * days)) + random.choice((-1, 0, 0, 0, 1))
    nn = max(60, min(99, nn))
    ref4 = random.choice(ref4_pool or ("0290", "0640", "1800", "2650", "3470", "4130",
                                       "4780", "6091", "6380", "8211", "8241", "9021",
                                       "9642", "9732"))
    p15 = random.choice("0123456789" * 3 + "ABCDEFGHJKLMNPRSTUVWXYZ")
    flag = random.choice("GB")
    route = random.choice(("1003", "1004", "1006", "1007", "1012", "1016", "1018", "1020"))
    tail = random.choice(("0501", "0902", "0301", "0901", "0703", "0502", "1101"))
    return "%s%04d%s%s%s0%s%s0011%02d%s" % (
        marker, ydoy, core.strftime("%H%M%S"), ref4, p15, flag, route, nn, tail)


# ───────────────────────────── сборка файла ─────────────────────────────
def _deflate(data: bytes) -> bytes:
    return zlib.compress(data, 6)


def build_sbp(
    *,
    when_msk: _dt.datetime,
    recipient: str,
    phone: str,
    bank: str,
    sender: str,
    account: str,
    amount: str,
    fee: str = "0.00",
    op_id: str,
    created_msk: Optional[_dt.datetime] = None,
    subset_tag: Optional[str] = None,
    file_id: Optional[Tuple[bytes, bytes]] = None,
) -> bytes:
    """Собрать чек. Все строки пишутся как есть (любые символы и длина)."""
    d = _load()
    tpl: bytes = d["template"]  # type: ignore[assignment]
    objs = _objects(tpl)
    # нормализуем сумму/комиссию: «<число>  ₽» (два пробела и знак рубля — как в оригинале)
    values = {
        "date": format_face_date(when_msk),
        "recipient": recipient,
        "phone": phone,
        "bank": bank,
        "sender": sender,
        "account": account,
        "amount": amount if amount.endswith("\u20bd") else amount + "  \u20bd",
        "fee": fee if fee.endswith("\u20bd") else fee + "  \u20bd",
        "opid": op_id,
    }
    # content
    h7, d7, t7 = _split_stream(objs[7])
    cs_tpl = zlib.decompress(d7)
    cs = build_content(cs_tpl, values, "")
    used_cids = sorted({c for k, v in values.items() for c in text_to_cids(v)} |
                       {c for c in _static_cids(cs_tpl)})
    used_cids = [c for c in used_cids if c != _NOTDEF_CID or True]
    # font
    font = build_font(used_cids)
    comp_used = sorted(set(used_cids))
    uni_by_cid = {c: u for c, u in _gid_uni_map().items() if c in set(comp_used)}
    w_arr = build_w_array(comp_used)
    tou = build_tounicode(comp_used, uni_by_cid)
    tag = subset_tag or _rand_tag()
    fname = (tag + "+ArialMT").encode()

    def restream(obj_no: int, new_data: bytes, extra: Optional[Dict[bytes, bytes]] = None) -> bytes:
        h, _d, t = _split_stream(objs[obj_no])
        comp = _deflate(new_data)
        h = re.sub(rb"/Length (\d+)", b"/Length %d" % len(comp), h)
        if extra:
            for k, v in extra.items():
                h = re.sub(k, v, h)
        return h + comp + t

    new: Dict[int, bytes] = {}
    for n in (3, 4, 5, 6, 1, 9, 14, 15, 16, 8):
        new[n] = objs[n]
    new[7] = restream(7, cs)
    new[10] = restream(10, font, {rb"/Length1 \d+": b"/Length1 %d" % len(font)})
    new[13] = restream(13, tou)
    o11 = objs[11]
    o11 = re.sub(rb"/FontName/[A-Z]{6}\+ArialMT", b"/FontName/" + fname, o11)
    new[11] = o11
    o12 = objs[12]
    o12 = re.sub(rb"/BaseFont/[A-Z]{6}\+ArialMT", b"/BaseFont/" + fname, o12)
    o12 = re.sub(rb"/W \[.*\]/CIDToGIDMap", b"/W " + w_arr + b"/CIDToGIDMap", o12, flags=re.S)
    new[12] = o12
    new[2] = re.sub(rb"/BaseFont/[A-Z]{6}\+ArialMT", b"/BaseFont/" + fname, objs[2])
    created = created_msk or (when_msk + _dt.timedelta(seconds=random.randint(61, 199)))
    cd = created.strftime("%Y%m%d%H%M%S").encode()
    o17 = re.sub(rb"/ModDate\(D:\d{14}", b"/ModDate(D:" + cd, objs[17])
    o17 = re.sub(rb"/CreationDate\(D:\d{14}", b"/CreationDate(D:" + cd, o17)
    new[17] = o17
    # порядок объектов в файле — как у iText
    order = [3, 4, 5, 6, 7, 1, 9, 10, 11, 12, 13, 2, 8, 14, 15, 16, 17]
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offs = {}
    for n in order:
        offs[n] = len(out)
        body = new[n]
        if not body.endswith(b"\n"):
            body += b"\n"
        out += body
    xref_pos = len(out)
    out += b"xref\n0 18\n0000000000 65535 f \n"
    for n in range(1, 18):
        out += b"%010d 00000 n \n" % offs[n]
    a, b = file_id or (_rand_file_id(), _rand_file_id())
    out += b"trailer\n<</Info 17 0 R/ID [<" + a + b"><" + b + b">]/Root 16 0 R/Size 18>>\nstartxref\n"
    out += b"%d\n%%%%EOF\n" % xref_pos
    return bytes(out)


def build_phone(
    *,
    when_msk: _dt.datetime,
    recipient: str,
    phone: str,
    recv_label: str,
    recv_account: str,
    sender: str,
    account: str,
    amount: str,
    fee: str,
    doc: str,
    auth: str,
    created_msk: Optional[_dt.datetime] = None,
    subset_tag: Optional[str] = None,
    file_id: Optional[Tuple[bytes, bytes]] = None,
) -> bytes:
    """Чек «по номеру телефона». Строки пишутся как есть, сборка как у iText."""
    tpl = _phone_template()
    objs = _objects(tpl)
    values = {
        "date": format_phone_date(when_msk),
        "recipient": recipient,
        "phone": phone,
        "recv_label": recv_label,
        "recv_account": recv_account,
        "sender": sender,
        "account": account,
        "amount": amount if amount.endswith("\u20bd") else amount + " \u20bd",
        "fee": fee if fee.endswith("\u20bd") else fee + " \u20bd",
        "doc": doc,
        "auth": auth,
    }
    _h, data, _t = _split_stream(objs[6])
    cs_tpl = zlib.decompress(data)
    cs = build_content(cs_tpl, values, "", ymap=_PHONE_Y)
    used_cids = sorted(set(_all_literal_cids(cs)))
    font = build_font(used_cids)
    comp_used = sorted(set(used_cids))
    uni_by_cid = {c: u for c, u in _gid_uni_map().items() if c in set(comp_used)}
    w_arr = build_w_array(comp_used)
    tou = build_tounicode(comp_used, uni_by_cid)
    tag = subset_tag or _rand_tag()
    fname = (tag + "+ArialMT").encode()

    def restream(obj_no: int, new_data: bytes, extra: Optional[Dict[bytes, bytes]] = None) -> bytes:
        h, _d, t = _split_stream(objs[obj_no])
        comp = _deflate(new_data)
        h = re.sub(rb"/Length (\d+)", b"/Length %d" % len(comp), h)
        if extra:
            for k, v in extra.items():
                h = re.sub(k, v, h)
        return h + comp + t

    new: Dict[int, bytes] = {n: objs[n] for n in (3, 4, 5, 1, 8, 7, 13, 14, 15)}
    new[6] = restream(6, cs)
    new[9] = restream(9, font, {rb"/Length1 \d+": b"/Length1 %d" % len(font)})
    new[12] = restream(12, tou)
    new[10] = re.sub(rb"/FontName/[A-Z]{6}\+ArialMT", b"/FontName/" + fname, objs[10])
    o11 = re.sub(rb"/BaseFont/[A-Z]{6}\+ArialMT", b"/BaseFont/" + fname, objs[11])
    o11 = re.sub(rb"/W \[.*\]/CIDToGIDMap", b"/W " + w_arr + b"/CIDToGIDMap", o11, flags=re.S)
    new[11] = o11
    new[2] = re.sub(rb"/BaseFont/[A-Z]{6}\+ArialMT", b"/BaseFont/" + fname, objs[2])
    created = created_msk or (when_msk + _dt.timedelta(seconds=random.randint(61, 199)))
    cd = created.strftime("%Y%m%d%H%M%S").encode()
    o16 = re.sub(rb"/ModDate\(D:\d{14}", b"/ModDate(D:" + cd, objs[16])
    new[16] = re.sub(rb"/CreationDate\(D:\d{14}", b"/CreationDate(D:" + cd, o16)
    order = [3, 4, 5, 6, 1, 8, 9, 10, 11, 12, 2, 7, 13, 14, 15, 16]
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offs = {}
    for n in order:
        offs[n] = len(out)
        body = new[n]
        if not body.endswith(b"\n"):
            body += b"\n"
        out += body
    xref_pos = len(out)
    out += b"xref\n0 17\n0000000000 65535 f \n"
    for n in range(1, 17):
        out += b"%010d 00000 n \n" % offs[n]
    a, b = file_id or (_rand_file_id(), _rand_file_id())
    out += b"trailer\n<</Info 16 0 R/ID [<" + a + b"><" + b + b">]/Root 15 0 R/Size 17>>\nstartxref\n"
    out += b"%d\n%%%%EOF\n" % xref_pos
    return bytes(out)


def _static_cids(cs: bytes, ymap: Optional[Dict[str, str]] = None) -> List[int]:
    """CID из постоянных блоков шаблона (подписи полей)."""
    blocks = _find_tj_blocks(cs)
    keep = set((ymap or _Y).values())
    res: List[int] = []
    for b in blocks:
        if b[4] in keep:
            continue
        raw = _unescape_literal(cs[b[2]:b[3]])
        res += [raw[i] << 8 | raw[i + 1] for i in range(0, len(raw) - 1, 2)]
    return res


def _all_literal_cids(cs: bytes) -> List[int]:
    """Все CID из строковых литералов, включая Tj без повторного Tf в том же BT."""
    res: List[int] = []
    i = 0
    n = len(cs)
    while i < n:
        if cs[i] != 0x28:
            i += 1
            continue
        j = i + 1
        buf = bytearray()
        while j < n:
            if cs[j] == 0x5C and j + 1 < n:
                buf += cs[j:j + 2]
                j += 2
                continue
            if cs[j] == 0x29:
                break
            buf.append(cs[j])
            j += 1
        raw = _unescape_literal(bytes(buf))
        res += [raw[k] << 8 | raw[k + 1] for k in range(0, len(raw) - 1, 2)]
        i = j + 1
    return res
