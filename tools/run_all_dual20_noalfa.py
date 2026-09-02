# -*- coding: utf-8 -*-
"""Dual gate (Proton+OnlyPDF) strict 20/20 for T-Bank + Sber (no Alfa).

Runs each method sequentially until 20 consecutive PASS, then next.
On FAKE: fix generator, deploy, re-run same method with --keep.

Usage:
  python tools/run_all_dual20_noalfa.py
  python tools/run_all_dual20_noalfa.py --start tbank_phone
  python tools/run_all_dual20_noalfa.py --only tbank_sbp,sber_sbp
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
_OUT = _ROOT / "output" / "dual20_noalfa"
_LOG = _OUT / "master.log"

METHODS = [
    ("gen_onlypdf30.py", "tbank_sbp", "tbank_sbp"),
    ("gen_onlypdf30_card_sber.py", "tbank_card_sber", "tbank_card_sber"),
    ("gen_onlypdf30_card_tbank.py", "tbank_card_tbank", "tbank_card_tbank"),
    ("gen_onlypdf30_phone.py", "tbank_phone", "tbank_phone"),
    ("gen_onlypdf30_nocomm.py", "tbank_nocomm", "tbank_nocomm"),
    ("gen_onlypdf30_sber_sbp.py", "sber_sbp", "sber_sbp"),
    ("gen_onlypdf30_sber_phone.py", "sber_phone", "sber_phone"),
]


def _log(msg: str) -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _LOG.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def _run_method(script: str, prefix: str, out_name: str, *, keep: bool) -> int:
    out_dir = _OUT / out_name
    py = _ROOT / "tools" / script
    cmd = [
        sys.executable,
        str(py),
        "-n",
        "20",
        "--strict",
        "--gate",
        "dual",
        "--out",
        str(out_dir),
        "--max-streak-breaks",
        "50",
    ]
    if keep:
        cmd.append("--keep")
    _log(f"RUN {' '.join(cmd)}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["TG_GATE"] = "dual"
    proc = subprocess.run(cmd, cwd=str(_ROOT), env=env)
    return int(proc.returncode)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="", help="method out_name to start from")
    ap.add_argument("--only", default="", help="comma out_names subset")
    ap.add_argument("--keep", action="store_true", help="keep partial progress on retry")
    args = ap.parse_args()

    methods = list(METHODS)
    if args.only:
        only = {x.strip() for x in args.only.split(",") if x.strip()}
        methods = [m for m in methods if m[2] in only]
    if args.start:
        names = [m[2] for m in methods]
        if args.start in names:
            methods = methods[names.index(args.start) :]

    _log(f"DUAL20 NOALFA start methods={[m[2] for m in methods]}")
    for script, prefix, out_name in methods:
        rc = _run_method(script, prefix, out_name, keep=args.keep)
        if rc != 0:
            _log(f"STOP {out_name} rc={rc} — fix+deploy then resume --start {out_name} --keep")
            return rc
        _log(f"DONE {out_name} 20/20")
    _log("ALL DUAL20 NOALFA COMPLETE")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
