# -*- coding: utf-8 -*-
"""Resume dual 30/30 from Alfa channels onward."""
import json, subprocess, sys, time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIR = Path(__file__).resolve().parent
PY = sys.executable
LOG = ROOT / "output" / "onlypdf_full_run"
LIVE = LOG / "live.log"
CHANNELS = [
    ("alfa_sbp", "gen_onlypdf30_alfa_sbp.py", "_test30_alfa_sbp", 6),
    ("alfa_card", "gen_onlypdf30_alfa_card.py", "_test30_alfa_card", 6),
    ("alfa_phone", "gen_onlypdf30_alfa_phone.py", "_test30_alfa_phone", 6),
    ("sber_sbp", "gen_onlypdf30_sber_sbp.py", "_test30_sber_sbp", 6),
    ("sber_phone", "gen_onlypdf30_sber_phone.py", "_test30_sber_phone", 6),
]

def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.mkdir(parents=True, exist_ok=True)
    with LIVE.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")

def run_one(label, script, out_name, attempt):
    out = ROOT / out_name
    log_path = LOG / f"{label}_a{attempt}.log"
    log(f"START {label} attempt={attempt} → {out} (log {log_path.name})")
    t0 = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as logf:
        proc = subprocess.Popen(
            [PY, "-u", str(DIR / script), "-n", "30", "--out", str(out)],
            cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
            env={**dict(**{k: v for k, v in __import__("os").environ.items()}),
                 "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
        rc = proc.wait()
    dt = round(time.time() - t0, 1)
    log(f"END {label} attempt={attempt} rc={rc} in {dt}s")
    try:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]:
            log(f"  | {line}")
    except Exception:
        pass
    return rc

def main():
    log("=" * 60)
    log("RESUME Dual OnlyPDF+Proton from Alfa (tbank already PASS)")
    log("=" * 60)
    results = []
    ok_all = True
    for label, script, out_name, max_retries in CHANNELS:
        ok = False
        last_rc = 1
        for attempt in range(1, max_retries + 1):
            last_rc = run_one(label, script, out_name, attempt)
            if last_rc == 0:
                ok = True
                break
            log(f"RETRY {label} after rc={last_rc}")
            time.sleep(5)
        results.append({"channel": label, "ok": ok, "rc": last_rc})
        if not ok:
            ok_all = False
            log(f"FATAL channel failed: {label}")
    summary = {
        "finished": datetime.now().isoformat(),
        "ok": ok_all,
        "gate": "onlypdf+proton",
        "mode": "resume_alfa+",
        "channels": results,
        "passed": sum(1 for r in results if r["ok"]),
        "total": len(results),
    }
    (LOG / "summary_resume.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"SUMMARY {summary['passed']}/{summary['total']} → summary_resume.json")
    return 0 if ok_all else 1

if __name__ == "__main__":
    raise SystemExit(main())
