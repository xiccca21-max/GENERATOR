# -*- coding: utf-8 -*-
"""Offline full-charset shell bake for live channels.

Used by night_proton_seq on glyph miss / visual tofu.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

logger = logging.getLogger(__name__)

_PY = sys.executable


def bake_channel(channel: str) -> bool:
    """Rebuild full-charset shell for channel. Returns True on success."""
    ch = (channel or "").strip().lower()
    try:
        if ch in ("tbank_phone", "phone"):
            r = subprocess.run(
                [_PY, "-u", str(_ROOT / "tools" / "_rebuild_phone_unlocked_clean.py")],
                cwd=str(_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            logger.info("bake tbank_phone rc=%s %s", r.returncode, (r.stdout or "")[-200:])
            return r.returncode == 0
        if ch in ("tbank_sbp", "sbp"):
            from tbank_sbp_stealth import build_sbp_tbank_unlocked_template

            build_sbp_tbank_unlocked_template()
            return True
        if ch.startswith("sber"):
            from sber_unlock_template import build_runtime_shell

            build_runtime_shell(force_library=True)
            return True
        if ch.startswith("alfa"):
            sys.path.insert(0, str(_ROOT / "tools"))
            from alfa_shell_bake import bake_alfa_channel

            return bake_alfa_channel(ch)
    except Exception as exc:
        logger.exception("bake_channel(%s): %s", ch, exc)
        return False
    return False


def deploy_changed(paths: Optional[list] = None) -> bool:
    """Deploy receipt-bot with changed files (or full if None)."""
    cmd = [_PY, "-u", str(_ROOT / "deploy" / "deploy.py")]
    if paths:
        cmd.extend(paths)
    else:
        cmd.append("--full")
    try:
        r = subprocess.run(
            cmd,
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        ok = r.returncode == 0 and "DEPLOY_OK" in (r.stdout or "")
        logger.info("deploy rc=%s ok=%s", r.returncode, ok)
        return ok
    except Exception as exc:
        logger.exception("deploy: %s", exc)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    target = sys.argv[1] if len(sys.argv) > 1 else "tbank_phone"
    raise SystemExit(0 if bake_channel(target) else 1)
