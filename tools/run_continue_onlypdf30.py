# -*- coding: utf-8 -*-
from __future__ import annotations
import json, subprocess, sys, time
from datetime import datetime
from pathlib import Path
_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
_PY = sys.executable
_LOG_DIR = _ROOT / "output" / "onlypdf_full_run"
_LIVE = _LOG_DIR / "live.log"
CHANNELS = [
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
def _run_one(label, script, out_name, attempt):
    out = _ROOT / out_name
    log_path = _LOG_DIR / f"{label}_retry_a{attempt}.log"
    _log(f"RETRY-SUITE START {label} attempt={attempt} → {out}")
    cmd = [_PY, "-u", str(_DIR / script), "-n", "30", "--out", str(out)]
    t0 = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as logf:
        proc = subprocess.Popen(cmd, cwd=str(_ROOT), stdout=logf, stderr=subprocess.STDOUT,
            env={**{k:v for k,v in __import__('os').environ.items()}, "PYTHONIOENCODING":"utf-8","PYTHONUNBUFFERED":"1"})
        rc = proc.wait()
    _log(f"RETRY-SUITE END {label} attempt={attempt} rc={rc} in {round(time.time()-t0,1)}s")
    try:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]:
            _log(f"  | {line}")
    except Exception:
        pass
    return rc
def main():
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log("=" * 60)
    _log("CONTINUE dual after service-error + recheck-reject fix")
    _log("=" * 60)
    results = []
    ok_all = True
    for label, script, out_name, max_retries in CHANNELS:
        ok = False
        last = 1
        for attempt in range(1, max_retries+1):
            last = _run_one(label, script, out_name, attempt)
            if last == 0:
                ok = True
                break
            _log(f"RETRY {label} after rc={last}")
            time.sleep(5)
        results.append({"channel": label, "ok": ok, "rc": last})
        if not ok:
            ok_all = False
    summary = {"finished": datetime.now().isoformat(), "ok": ok_all, "channels": results,
               "note": "alfa_sbp+alfa_card already 30/30 earlier"}
    (_LOG_DIR / "continue_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"SUMMARY ok={ok_all} → continue_summary.json")
    return 0 if ok_all else 1
if __name__ == "__main__":
    raise SystemExit(main())
