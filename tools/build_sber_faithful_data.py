# -*- coding: utf-8 -*-
"""Собрать sber_faithful_data/ из настоящих банковских чеков СБП (Jasper/iText 2.1.7).

Источники глифов: любые PDF (папки передаются аргументами), где встроен шрифт Arial с теми же
fpgm/prep/cvt/hmtx/maxp, что у эталона. Байты глифа на один GID должны совпадать во всех файлах
(иначе скрипт падает: корпус смешивает версии шрифта).

Запуск:
    python tools/build_sber_faithful_data.py <genuine_dir> [<extra_dir> ...]
"""
import base64
import collections
import hashlib
import json
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "sber_faithful_data")
NG = 3419
KEYS = ("fpgm", "prep", "cvt ", "hmtx", "maxp")


def read_tables(x):
    n = struct.unpack(">H", x[4:6])[0]
    t = {}
    for i in range(n):
        tag, _c, off, ln = struct.unpack(">4sIII", x[12 + 16 * i:28 + 16 * i])
        t[tag.decode("latin-1")] = x[off:off + ln]
    return t


def fonts_in(pdf):
    for m in re.finditer(rb"stream\r?\n(x[\x01\x5e\x9c\xda])", pdf):
        s = m.start(1)
        e = pdf.find(b"endstream", s)
        try:
            x = zlib.decompressobj().decompress(pdf[s:e])
        except Exception:
            continue
        if x[:4] == b"\x00\x01\x00\x00" and len(x) > 3000:
            yield x


def _skip_op(ins, i):
    op = ins[i]
    if op == 0x40:
        return i + 2 + ins[i + 1]
    if op == 0x41:
        return i + 2 + 2 * ins[i + 1]
    if 0xB0 <= op <= 0xB7:
        return i + 1 + (op - 0xB0 + 1)
    if 0xB8 <= op <= 0xBF:
        return i + 1 + 2 * (op - 0xB8 + 1)
    return i + 1


def _if_branch(ins):
    """Windows Arial v7 оборачивает программу: PUSHB 0x85 CALL IF <программа> [ELSE ...] EIF."""
    if not ins.startswith(bytes.fromhex("b0852b58")):
        return ins
    depth = 0
    i = 4
    while i < len(ins):
        op = ins[i]
        if op == 0x58:
            depth += 1
        elif op == 0x1B and depth == 0:
            return ins[4:i]
        elif op == 0x59:
            if depth == 0:
                return ins[4:i]
            depth -= 1
        i = _skip_op(ins, i)
    return ins


def _simple_tail_len(raw, ip_after_instr):
    """Длина flags+coords простого глифа без хвостового выравнивания."""
    nc = struct.unpack(">h", raw[:2])[0]
    ep = struct.unpack(">%dH" % nc, raw[10:10 + 2 * nc])
    npts = ep[-1] + 1
    i = ip_after_instr
    flags = []
    while len(flags) < npts:
        f = raw[i]; i += 1
        flags.append(f)
        if f & 8:
            r = raw[i]; i += 1
            flags.extend([f] * r)
    xs = sum(1 if f & 2 else (0 if f & 16 else 2) for f in flags)
    ys = sum(1 if f & 4 else (0 if f & 32 else 2) for f in flags)
    return i + xs + ys


def synth_glyph(raw):
    """Запись глифа Windows Arial -> форма банковского шрифта (программа без обёртки v7)."""
    nc = struct.unpack(">h", raw[:2])[0]
    if nc < 0:
        return raw  # составные: у оригиналов совпадают с Windows побайтно
    ip = 10 + 2 * nc
    il = struct.unpack(">H", raw[ip:ip + 2])[0]
    ins = raw[ip + 2:ip + 2 + il]
    end = _simple_tail_len(raw, ip + 2 + il)
    body = raw[ip + 2 + il:end]
    new = _if_branch(ins)
    out = raw[:ip] + struct.pack(">H", len(new)) + new + body
    if len(out) % 2:
        out += b"\0"
    return out


def build_synth(win, uni2gid, genuine):
    """Приближённые записи для GID, которых нет в настоящих файлах (помечаются как approx)."""
    glyf_raw = win.reader["glyf"]
    loca = win["loca"].locations

    def rec(g):
        return glyf_raw[loca[g]:loca[g + 1]]

    def comps(raw):
        if len(raw) < 10 or struct.unpack(">h", raw[:2])[0] >= 0:
            return []
        i, res = 10, []
        while True:
            fl, gi = struct.unpack(">HH", raw[i:i + 4])
            res.append(gi)
            i += 4 + (4 if fl & 1 else 2)
            if fl & 8:
                i += 2
            elif fl & 0x40:
                i += 4
            elif fl & 0x80:
                i += 8
            if not fl & 0x20:
                break
        return res

    synth = {}
    todo = sorted(set(uni2gid.values()))
    todo.append(3418)  # ₽ есть в оригиналах; здесь только страховка
    seen = set()
    while todo:
        g = todo.pop()
        if g in seen or g in genuine or g >= NG or g >= len(loca) - 1:
            continue
        seen.add(g)
        raw = rec(g)
        if not raw:
            continue
        synth[g] = synth_glyph(raw)
        todo.extend(comps(raw))
    return synth


def main():
    dirs = sys.argv[1:]
    if not dirs:
        raise SystemExit(__doc__)
    ref_pdf = None
    for root in dirs:
        for dp, _dn, fn in os.walk(root):
            for f in sorted(fn):
                if f.lower().endswith(".pdf") and "sbp_outgoing" in dp:
                    ref_pdf = os.path.join(dp, f)
                    break
            if ref_pdf:
                break
        if ref_pdf:
            break
    if not ref_pdf:
        raise SystemExit("нужен каталог sbp_outgoing с настоящими чеками СБП (шаблон)")
    ref = next(fonts_in(open(ref_pdf, "rb").read()))
    rt = read_tables(ref)
    sig = tuple(hashlib.md5(rt[k]).hexdigest() for k in KEYS)
    store = {}
    nfonts = 0
    for root in dirs:
        for dp, _dn, fn in os.walk(root):
            for f in fn:
                if not f.lower().endswith(".pdf"):
                    continue
                try:
                    pdf = open(os.path.join(dp, f), "rb").read()
                except OSError:
                    continue
                for x in fonts_in(pdf):
                    try:
                        t = read_tables(x)
                        if tuple(hashlib.md5(t[k]).hexdigest() for k in KEYS) != sig:
                            continue
                        loca = struct.unpack(">%dI" % (NG + 1), t["loca"])
                        gl = t["glyf"]
                    except Exception:
                        continue
                    nfonts += 1
                    for g in range(NG):
                        if loca[g + 1] > loca[g]:
                            raw = gl[loca[g]:loca[g + 1]]
                            if store.setdefault(g, raw) != raw:
                                raise SystemExit("конфликт глифа GID %d (%s)" % (g, f))
    base = {k: base64.b64encode(v).decode() for k, v in rt.items() if k not in ("glyf", "loca")}
    from fontTools.ttLib import TTFont
    win = TTFont(os.environ.get("WIN_ARIAL", r"C:\Windows\Fonts\arial.ttf"))
    order = win.getGlyphOrder()
    idx = {n: i for i, n in enumerate(order)}
    uni2gid = {}
    for u, n in win.getBestCmap().items():
        g = idx[n]
        if g < NG:
            uni2gid[str(u)] = g
    synth = build_synth(win, uni2gid, store)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "arial_glyphs_synth.json"), "w", encoding="utf-8") as fp:
        json.dump({str(g): v.hex() for g, v in sorted(synth.items())}, fp)
    with open(os.path.join(OUT, "arial_base.json"), "w", encoding="utf-8") as fp:
        json.dump({"tables": base, "uni2gid": uni2gid}, fp)
    with open(os.path.join(OUT, "arial_glyphs.json"), "w", encoding="utf-8") as fp:
        json.dump({str(g): v.hex() for g, v in sorted(store.items())}, fp)
    with open(os.path.join(OUT, "sbp_template.pdf"), "wb") as fp:
        fp.write(open(ref_pdf, "rb").read())
    print("fonts", nfonts, "glyphs", len(store), "uni", len(uni2gid), "template", ref_pdf)


if __name__ == "__main__":
    main()
