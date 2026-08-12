# -*- coding: utf-8 -*-
"""Shared CLI for gen_onlypdf30_*.py — one gate, one generator family."""
from __future__ import annotations

import argparse
from pathlib import Path


def parse_batch_args(
    *,
    default_out: Path,
    default_n: int = 30,
    description: str = "OnlyPDF-gated batch",
) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("-n", type=int, default=default_n, help="How many PASS PDFs (default 30)")
    ap.add_argument(
        "--out",
        type=Path,
        default=default_out,
        help=f"Output folder (default: {default_out})",
    )
    ap.add_argument(
        "--keep",
        action="store_true",
        help="Do not wipe out-dir before run (default: fresh wipe)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Strict streak: FAKE/UNKNOWN resets series to 0",
    )
    ap.add_argument(
        "--max-streak-breaks",
        type=int,
        default=30,
        help="Max STREAK_BREAK before FATAL (strict mode)",
    )
    ap.add_argument(
        "--gate",
        choices=("onlypdf", "fraudex"),
        default="onlypdf",
        help="Telegram checker: OnlyPDF (default) or Fraudex (separate session)",
    )
    args = ap.parse_args()
    if args.gate == "fraudex":
        import os

        os.environ["TG_GATE"] = "fraudex"
    return args
