# -*- coding: utf-8 -*-
"""Resume OnlyPDF-only 30/30 from Sber (Proton OFF; Alfa skipped)."""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIR = Path(__file__).resolve().parent
PY = sys.executable
LOG = ROOT / "output" / "onlypdf_full_run"
LIVE = LOG / "live.log"
CHANNELS = [
    ("sber_sbp", "gen_onlypdf30_sber_sbp.py", "_test30_sber_sbp", 4),
    ("sber_phone", "gen_onlypdf30_sber_phone.py", "_test30_sber_phone", 4),
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
            cwd=str(ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            env={
                **dict(**{k: v for k, v in __import__("os").environ.items()}),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                "TG_WAIT_SECONDS": "25",
                "TG_POLL_SECONDS": "0.5",
            },
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
    log("RESUME OnlyPDF-only from Sber (Proton OFF, ~30s/check)")
    log("SKIP alfa_*: OnlyPDF SERVICE_ERROR even on real originals")
    log("=" * 60)
    results = [
        {
            "channel": "alfa_*",
            "ok": False,
            "rc": 99,
            "skipped": "onlypdf_alfa_service_error",
        }
    ]
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
            time.sleep(2)
        results.append({"channel": label, "ok": ok, "rc": last_rc})
        if not ok:
            ok_all = False
            log(f"FATAL channel failed: {label}")
    summary = {
        "ok_all": ok_all,
        "gate": "onlypdf-only",
        "results": results,
        "finished": datetime.now().isoformat(timespec="seconds"),
    }
    (LOG / "resume_sber_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"SUMMARY ok_all={ok_all} gate=onlypdf-only")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
