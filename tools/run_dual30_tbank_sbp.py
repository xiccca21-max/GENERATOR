# -*- coding: utf-8 -*-
"""T-Bank SBP: 30/30 strict streak — Proton + OnlyPDF both PASS per file.

Canonical generator: create_tbank_sbp_stealth (same as bot.py).

Usage:
  python tools/run_dual30_tbank_sbp.py
  python tools/run_dual30_tbank_sbp.py -n 5 --out output/tbank_sbp_dual5

Requires: tools/tg_check.env + proxy, tools/.tg_checker_session
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_DIR))

from gen_onlypdf30 import _f1_ok, _gen, _payload  # noqa: E402
from onlypdf_batch_cli import parse_batch_args  # noqa: E402
from onlypdf_gate import generate_onlypdf_batch  # noqa: E402


async def main() -> int:
    args = parse_batch_args(
        default_out=_ROOT / "output" / "tbank_sbp_dual30",
        description="T-Bank SBP — Proton+OnlyPDF dual 30/30 strict streak",
    )
    # Force dual gate (overrides default onlypdf in parse_batch_args if not passed)
    import os

    os.environ["TG_GATE"] = "dual"

    return await generate_onlypdf_batch(
        out_dir=args.out,
        n=args.n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=_f1_ok,
        prefix="tbank_sbp",
        max_attempts=20,
        fresh=not args.keep,
        full_recheck=True,
        strict_streak=True,
        max_streak_breaks=args.max_streak_breaks,
    )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
