# -*- coding: utf-8 -*-
"""Fixed-point: чек «по телефону», собранный из данных оригинала, совпадает побайтно.

    python tools/test_sber_phone_faithful_fixed_point.py <genuine_phone_dir>
"""
import datetime as dt
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sber_sbp_faithful as F  # noqa: E402

MON = {m: i + 1 for i, m in enumerate(F._MONTHS)}


def content_of(pdf):
    best = None
    for m in re.finditer(rb"stream\r?\n", pdf):
        s = m.end()
        e = pdf.find(b"endstream", s)
        try:
            x = zlib.decompress(pdf[s:e].rstrip(b"\r\n"))
        except Exception:
            continue
        if b"/F1" in x and b"Tj" in x and (best is None or len(x) > len(best)):
            best = x
    return best


def fields_of(pdf):
    cs = content_of(pdf)
    gid2uni = F._gid_uni_map()
    by_y = {}
    for x0, x1, s0, s1, y in F._find_tj_blocks(cs):
        raw = F._unescape_literal(cs[s0:s1])
        cids = [raw[i] << 8 | raw[i + 1] for i in range(0, len(raw) - 1, 2)]
        by_y[y] = "".join(chr(gid2uni.get(c, 0xFFFD)) for c in cids)
    v = {k: by_y[y] for k, y in F._PHONE_Y.items()}
    m = re.match(r"(\d+) (\S+) (\d+) (\d+):(\d+):(\d+)", v["date"])
    when = dt.datetime(int(m.group(3)), MON[m.group(2)], int(m.group(1)),
                       int(m.group(4)), int(m.group(5)), int(m.group(6)))
    cd = re.search(rb"/CreationDate\(D:(\d{14})", pdf).group(1).decode()
    created = dt.datetime.strptime(cd, "%Y%m%d%H%M%S")
    tag = re.search(rb"/BaseFont/([A-Z]{6})\+ArialMT", pdf).group(1).decode()
    ids = re.search(rb"/ID \[<([0-9a-f]+)><([0-9a-f]+)>", pdf)
    return dict(
        when_msk=when, recipient=v["recipient"], phone=v["phone"],
        recv_label=v["recv_label"], recv_account=v["recv_account"],
        sender=v["sender"], account=v["account"], amount=v["amount"], fee=v["fee"],
        doc=v["doc"], auth=v["auth"], created_msk=created, subset_tag=tag,
        file_id=(ids.group(1), ids.group(2)),
    )


def first_diff(a, b):
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return min(len(a), len(b))


def main():
    root = sys.argv[1]
    ok = bad = 0
    for f in sorted(os.listdir(root)):
        if not f.lower().endswith(".pdf"):
            continue
        orig = open(os.path.join(root, f), "rb").read()
        out = F.build_phone(**fields_of(orig))
        if out == orig:
            ok += 1
            print("EXACT ", f)
        else:
            bad += 1
            i = first_diff(out, orig)
            print("DIFF  ", f, "len", len(out), len(orig), "first diff @", i)
            print("   gen:", out[max(0, i - 30):i + 50])
            print("   our:", orig[max(0, i - 30):i + 50])
    print("exact %d / %d" % (ok, ok + bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
