# -*- coding: utf-8 -*-
"""Strict OnlyPDF 50-in-a-row: FAKE/UNKNOWN wipes the series (true consecutive greens).

Order: T-Bank → Sber → Alfa.
Success only if RESULT STREAK 50/50 FAKE=0 (accepted series has no FAKE skips).

Usage:
  python tools/run_strict50_all.py
  python tools/run_strict50_all.py --only tbank_sbp,sber_sbp
  python tools/run_strict50_all.py --only alfa_sbp,alfa_card,alfa_phone --force
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
_PY = sys.executable
_LOG_DIR = _ROOT / "output" / "onlypdf_full_run"
_SUMMARY = _LOG_DIR / "strict50_summary.json"
_LIVE = _LOG_DIR / "live.log"
_TARGET_N = 50

# T-Bank → Sber → Alfa (easier first).
CHANNELS = [
    ("tbank_sbp", "gen_onlypdf30.py", "_strict50_tbank_sbp"),
    ("tbank_card_sber", "gen_onlypdf30_card_sber.py", "_strict50_tbank_card_sber"),
    ("tbank_card_tbank", "gen_onlypdf30_card_tbank.py", "_strict50_tbank_card_tbank"),
    ("tbank_phone", "gen_onlypdf30_phone.py", "_strict50_tbank_phone"),
    ("tbank_nocomm", "gen_onlypdf30_nocomm.py", "_strict50_tbank_nocomm"),
    ("sber_sbp", "gen_onlypdf30_sber_sbp.py", "_strict50_sber_sbp"),
    ("sber_phone", "gen_onlypdf30_sber_phone.py", "_strict50_sber_phone"),
    ("alfa_sbp", "gen_onlypdf30_alfa_sbp.py", "_strict50_alfa_sbp"),
    ("alfa_card", "gen_onlypdf30_alfa_card.py", "_strict50_alfa_card"),
    ("alfa_phone", "gen_onlypdf30_alfa_phone.py", "_strict50_alfa_phone"),
]


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _LIVE.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def _parse_streak_result(log_text: str) -> dict:
    """Extract RESULT STREAK n/n FAKE=0 resets=N (or legacy breaks=N) from log."""
    m = re.search(
        r"RESULT STREAK (\d+)/(\d+) FAKE=(\d+) (?:resets|breaks)=(\d+)",
        log_text,
    )
    if not m:
        return {
            "ok": False,
            "streak": 0,
            "fake_in_streak": -1,
            "streak_breaks": -1,
            "streak_resets": -1,
        }
    got, need, fake, resets = (int(m.group(i)) for i in range(1, 5))
    # Accepted series is clean (FAKE=0). resets = how many wipes before success.
    return {
        "ok": got == need and fake == 0,
        "streak": got,
        "target": need,
        "fake_in_streak": fake,
        "streak_breaks": resets,  # legacy key
        "streak_resets": resets,
    }


def _channel_done(label: str, out_name: str) -> dict | None:
    """Resume: skip if summary already has ok streak for this channel."""
    if not _SUMMARY.is_file():
        return None
    try:
        data = json.loads(_SUMMARY.read_text(encoding="utf-8"))
    except Exception:
        return None
    for ch in data.get("channels") or []:
        if ch.get("channel") == label and ch.get("ok") and ch.get("fake_in_streak") == 0:
            out = _ROOT / out_name
            n_pdf = len(list(out.glob("*.pdf"))) if out.is_dir() else 0
            if n_pdf >= _TARGET_N and ch.get("streak", 0) >= _TARGET_N:
                return ch
    return None


def _run(label: str, script: str, out_name: str, attempt: int) -> tuple[int, dict]:
    out = _ROOT / out_name
    log_path = _LOG_DIR / f"strict50_{label}_a{attempt}.log"
    _log(f"START {label} attempt={attempt} → {out}")
    cmd = [
        _PY,
        "-u",
        str(_DIR / script),
        "-n",
        str(_TARGET_N),
        "--out",
        str(out),
        "--strict",
        "--max-streak-breaks",
        "200",
    ]
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
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    parsed = _parse_streak_result(log_text)
    parsed["rc"] = rc
    parsed["log"] = str(log_path)
    parsed["out"] = str(out)
    parsed["elapsed_s"] = round(time.time() - t0, 1)
    _log(
        f"END {label} attempt={attempt} rc={rc} "
        f"streak={parsed.get('streak')} fake={parsed.get('fake_in_streak')} "
        f"resets={parsed.get('streak_resets')} in {parsed['elapsed_s']}s"
    )
    return rc, parsed


def main() -> int:
    ap = argparse.ArgumentParser(description="Strict OnlyPDF 50 streak all banks")
    ap.add_argument(
        "--only",
        type=str,
        default="",
        help="Comma-separated channel labels to run",
    )
    ap.add_argument("--retries", type=int, default=3, help="Retries per channel")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-run even if summary already has ok streak",
    )
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}

    _log("STRICT 50 STREAK — wipe on FAKE/UNKNOWN (true consecutive greens)")
    results: list[dict] = []
    ok_all = True

    for label, script, out_name in CHANNELS:
        if only and label not in only:
            continue
        if not args.force:
            prev = _channel_done(label, out_name)
            if prev:
                _log(f"SKIP {label}: already streak={prev.get('streak')} FAKE=0")
                results.append({**prev, "channel": label, "skipped": True})
                continue

        ok = False
        last_parsed: dict = {"ok": False, "rc": 1}
        for attempt in range(1, args.retries + 1):
            _rc, last_parsed = _run(label, script, out_name, attempt)
            if last_parsed.get("ok") and last_parsed.get("rc") == 0:
                ok = True
                break
            # rc=5 = too many streak breaks — stop retrying same broken gen
            if last_parsed.get("rc") == 5:
                _log(f"STOP {label}: too many streak breaks — fix generator")
                break
            time.sleep(3)

        entry = {
            "channel": label,
            "ok": ok,
            **{k: v for k, v in last_parsed.items() if k != "ok"},
        }
        entry["ok"] = ok
        results.append(entry)
        if not ok:
            ok_all = False
            # Continue other channels so we see full picture; gen fixes later.

        summary = {
            "finished": datetime.now().isoformat(),
            "ok": ok_all and all(r.get("ok") for r in results),
            "target": _TARGET_N,
            "rule": "strict_streak_wipe_on_FAKE",
            "channels": results,
        }
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        _SUMMARY.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    summary = {
        "finished": datetime.now().isoformat(),
        "ok": ok_all and bool(results) and all(r.get("ok") for r in results),
        "target": _TARGET_N,
        "rule": "strict_streak_wipe_on_FAKE",
        "channels": results,
    }
    _SUMMARY.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _log(f"SUMMARY → {_SUMMARY} ok={summary['ok']}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
