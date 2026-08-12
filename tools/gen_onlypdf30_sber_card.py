# -*- coding: utf-8 -*-
"""Sber card→other — BLOCKED until OnlyPDF-clean donor exists.

Donor `сбер по карте в другой банк.pdf` is flagged by @onlypdf_robot as
virtual printer even as the original. No examples, no bot channel.
See tools/ONLYPDF_METHODS.md.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_DIR))


async def main() -> int:
    print(
        "BLOCKED: Sber card→other has no OnlyPDF-clean donor "
        "(original flagged as virtual printer). "
        "Do not generate. See tools/ONLYPDF_METHODS.md.",
        flush=True,
    )
    return 3


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
