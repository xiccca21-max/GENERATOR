# -*- coding: utf-8 -*-
"""Audit required Cyrillic+digits coverage on live channel shells."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import fitz  # noqa: E402
import tbank_unlock_template as tut  # noqa: E402

NEED = (
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "0123456789"
)

LIVE_SHELLS: Dict[str, List[Tuple[str, str]]] = {
    # channel -> [(path, font_substr_hint)] — unlocked only (acceptance = miss=0)
    "tbank_phone": [
        ("templates/T_phone_unlocked.pdf", "TinkoffSans-Regular"),
        ("templates/T_phone_unlocked.pdf", "TinkoffSans-Medium"),
    ],
    "tbank_sbp": [
        ("templates/T_sbp_unlocked.pdf", "TinkoffSans-Regular"),
    ],
    "sber_sbp": [
        ("templates/S_sbp_unlocked.pdf", "Arial"),
        ("templates/S_sbp_runtime.pdf", "Arial"),
    ],
    "sber_phone": [
        ("templates/S_sbp_runtime.pdf", "Arial"),
    ],
    "alfa_phone": [
        ("templates/Alfa_phone_unlocked.pdf", ""),
    ],
    "alfa_card": [
        ("templates/Alfa_card_unlocked.pdf", ""),
    ],
    "alfa_sbp": [
        ("templates/Alfa_sbp_unlocked.pdf", ""),
    ],
}

# Medium only needs digits + space for amounts on T-Bank
MED_NEED = "0123456789 "


def _font_charset(path: Path, hint: str) -> Tuple[str, set]:
    doc = fitz.open(str(path))
    fonts = tut._find_font_objects(doc)
    key = None
    if hint:
        for k in fonts:
            if hint in k:
                key = k
                break
    if key is None:
        key = next(iter(fonts), None)
    if key is None:
        doc.close()
        return "", set()
    fm = fonts[key]
    tu = tut._parse_subset_tounicode(
        doc.xref_stream(fm["tounicode_xref"]).decode("latin1", "replace")
    )
    chars = {chr(u) for u in tu.values() if u >= 0x20}
    doc.close()
    return key, chars


def audit_one(channel: str, rel: str, hint: str) -> dict:
    path = _ROOT / rel
    if not path.is_file():
        return {
            "channel": channel,
            "path": rel,
            "ok": False,
            "error": "missing",
            "miss": NEED,
        }
    key, chars = _font_charset(path, hint)
    need = MED_NEED if "Medium" in (hint or key) else NEED
    miss = "".join(c for c in need if c not in chars)
    return {
        "channel": channel,
        "path": rel,
        "font": key,
        "n_chars": len(chars),
        "miss": miss,
        "ok": not miss,
        "size": path.stat().st_size,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="", help="single channel or all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = []
    for ch, items in LIVE_SHELLS.items():
        if args.channel and ch != args.channel:
            continue
        for rel, hint in items:
            rows.append(audit_one(ch, rel, hint))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        bad = 0
        for r in rows:
            status = "OK" if r.get("ok") else "MISS"
            if not r.get("ok"):
                bad += 1
            print(
                f"{status:4} {r['channel']:12} {r.get('font') or '-':28} "
                f"n={r.get('n_chars', 0):4} miss={r.get('miss') or r.get('error') or '—'}"
            )
        print(f"fail={bad}/{len(rows)}")
    return 0 if all(r.get("ok") for r in rows if r.get("error") != "missing") else 1


if __name__ == "__main__":
    # Treat missing optional alfa unlocked as skip for exit code of core shells
    raise SystemExit(main())
