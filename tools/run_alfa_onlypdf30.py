# -*- coding: utf-8 -*-
"""Re-gate Alfa OnlyPDF 30/30 (SBP + card + phone) after MSK time fix."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
_PY = sys.executable
_LOG_DIR = _ROOT / "output" / "onlypdf_full_run"
_LIVE = _LOG_DIR / "live.log"
_SUMMARY = _LOG_DIR / "alfa_onlypdf_summary.json"

CHANNELS = [
    ("alfa_sbp", "gen_onlypdf30_alfa_sbp.py", "_test30_alfa_sbp", 3),
    ("alfa_card", "gen_onlypdf30_alfa_card.py", "_test30_alfa_card", 3),
    ("alfa_phone", "gen_onlypdf30_alfa_phone.py", "_test30_alfa_phone", 3),
]


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _LIVE.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def _run(label: str, script: str, out_name: str, attempt: int) -> int:
    out = _ROOT / out_name
    log_path = _LOG_DIR / f"{label}_msk_a{attempt}.log"
    _log(f"START {label} attempt={attempt} → {out}")
    cmd = [_PY, "-u", str(_DIR / script), "-n", "30", "--out", str(out)]
    t0 = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            env={
                **{k: v for k, v in __import__("os").environ.items()},
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            },
        )
        rc = proc.wait()
    _log(f"END {label} attempt={attempt} rc={rc} in {round(time.time()-t0,1)}s")
    return rc


def main() -> int:
    _log("ALFA OnlyPDF re-gate (MSK fix)")
    results = []
    ok_all = True
    for label, script, out_name, retries in CHANNELS:
        ok = False
        last = 1
        for attempt in range(1, retries + 1):
            last = _run(label, script, out_name, attempt)
            if last == 0:
                ok = True
                break
            time.sleep(5)
        results.append({"channel": label, "ok": ok, "rc": last})
        if not ok:
            ok_all = False
    summary = {
        "finished": datetime.now().isoformat(),
        "ok": ok_all,
        "channels": results,
    }
    _SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"SUMMARY → {_SUMMARY} ok={ok_all}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
