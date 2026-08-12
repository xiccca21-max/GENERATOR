# -*- coding: utf-8 -*-
"""Re-run channels that failed in the full dual suite."""
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

# Failed after T-Bank passed in the 2026-07-24 evening suite.
CHANNELS = [
    ("alfa_sbp", "gen_onlypdf30_alfa_sbp.py", "_test30_alfa_sbp", 8),
    ("alfa_card", "gen_onlypdf30_alfa_card.py", "_test30_alfa_card", 8),
    ("alfa_phone", "gen_onlypdf30_alfa_phone.py", "_test30_alfa_phone", 8),
    ("sber_sbp", "gen_onlypdf30_sber_sbp.py", "_test30_sber_sbp", 8),
    ("sber_phone", "gen_onlypdf30_sber_phone.py", "_test30_sber_phone", 8),
]


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _LIVE.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def _run_one(label: str, script: str, out_name: str, attempt: int) -> int:
    script_path = _DIR / script
    out = _ROOT / out_name
    log_path = _LOG_DIR / f"{label}_retry_a{attempt}.log"
    _log(f"RETRY-SUITE START {label} attempt={attempt} → {out}")
    cmd = [_PY, "-u", str(script_path), "-n", "30", "--out", str(out)]
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
    _log(f"RETRY-SUITE END {label} attempt={attempt} rc={rc} in {round(time.time() - t0, 1)}s")
    try:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]:
            _log(f"  | {line}")
    except Exception:
        pass
    return rc


def main() -> int:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log("=" * 60)
    _log(f"RETRY failed dual 30/30 — {len(CHANNELS)} channels")
    _log("=" * 60)
    results = []
    ok_all = True
    for label, script, out_name, max_retries in CHANNELS:
        ok = False
        last_rc = 1
        for attempt in range(1, max_retries + 1):
            last_rc = _run_one(label, script, out_name, attempt)
            if last_rc == 0:
                ok = True
                break
            _log(f"RETRY {label} after rc={last_rc}")
            time.sleep(5)
        results.append({"channel": label, "ok": ok, "rc": last_rc})
        if not ok:
            ok_all = False
            _log(f"FATAL channel failed: {label}")
    summary = {
        "finished": datetime.now().isoformat(),
        "ok": ok_all,
        "gate": "onlypdf+proton",
        "channels": results,
        "passed": sum(1 for r in results if r["ok"]),
        "total": len(results),
    }
    path = _LOG_DIR / "retry_failed_summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"SUMMARY {summary['passed']}/{summary['total']} → {path}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
