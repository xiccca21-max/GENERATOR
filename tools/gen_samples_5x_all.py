# -*- coding: utf-8 -*-
"""Build output/samples_5x_all_banks: 5 OnlyPDF-gated PDFs per live channel.

Uses canonical tools/gen_onlypdf30_*.py only (no ungated create_*).
Skips blocked Sber card.
"""
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
_OUT_ROOT = _ROOT / "output" / "samples_5x_all_banks"
_LOG = _OUT_ROOT / "run.log"

CHANNELS = [
    ("tbank_sbp", "gen_onlypdf30.py"),
    ("tbank_card_sber", "gen_onlypdf30_card_sber.py"),
    ("tbank_card_tbank", "gen_onlypdf30_card_tbank.py"),
    ("tbank_phone", "gen_onlypdf30_phone.py"),
    ("tbank_nocomm", "gen_onlypdf30_nocomm.py"),
    ("alfa_sbp", "gen_onlypdf30_alfa_sbp.py"),
    ("alfa_card", "gen_onlypdf30_alfa_card.py"),
    ("alfa_phone", "gen_onlypdf30_alfa_phone.py"),
    ("sber_sbp", "gen_onlypdf30_sber_sbp.py"),
    ("sber_phone", "gen_onlypdf30_sber_phone.py"),
]


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _OUT_ROOT.mkdir(parents=True, exist_ok=True)
    with _LOG.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def _run(label: str, script: str) -> int:
    out = _OUT_ROOT / label
    out.mkdir(parents=True, exist_ok=True)
    log_path = _OUT_ROOT / f"{label}.log"
    cmd = [_PY, "-u", str(_DIR / script), "-n", "5", "--out", str(out)]
    _log(f"START {label} → {out}")
    t0 = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            env={
                **dict(__import__("os").environ),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            },
        )
        rc = proc.wait()
    dt = round(time.time() - t0, 1)
    pdfs = sorted(out.glob("*.pdf"))
    sizes = [p.stat().st_size for p in pdfs]
    _log(
        f"END {label} rc={rc} in {dt}s pdfs={len(pdfs)} "
        f"sizes={sizes}"
    )
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]
        for line in tail:
            _log(f"  | {line}")
    except Exception:
        pass
    return rc


def _write_manifest(results: list) -> None:
    lines = [
        f"samples_5x_all_banks — {datetime.now().isoformat(timespec='seconds')}",
        "OnlyPDF-gated via tools/gen_onlypdf30_*.py (PASS required).",
        "Blocked skipped: sber_card",
        "",
    ]
    rows = []
    for r in results:
        folder = _OUT_ROOT / r["channel"]
        pdfs = sorted(folder.glob("*.pdf")) if folder.is_dir() else []
        for p in pdfs:
            rows.append((r["channel"], p.name, p.stat().st_size))
            lines.append(f"{r['channel']}/{p.name}\t{p.stat().st_size}")
        if not pdfs:
            lines.append(f"{r['channel']}/\t(empty) ok={r['ok']} rc={r['rc']}")
    lines.append("")
    lines.append(
        f"TOTAL pdfs={len(rows)} channels_ok="
        f"{sum(1 for r in results if r['ok'])}/{len(results)}"
    )
    (_OUT_ROOT / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (_OUT_ROOT / "summary.json").write_text(
        json.dumps(
            {
                "finished": datetime.now().isoformat(),
                "out": str(_OUT_ROOT),
                "channels": results,
                "pdf_count": len(rows),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    _OUT_ROOT.mkdir(parents=True, exist_ok=True)
    _log("=" * 60)
    _log(f"5× OnlyPDF samples → {_OUT_ROOT}")
    _log("=" * 60)
    results = []
    overall = True
    for label, script in CHANNELS:
        rc = _run(label, script)
        ok = rc == 0
        if not ok:
            overall = False
        results.append({"channel": label, "script": script, "ok": ok, "rc": rc})
        # brief pause between channels to ease Telegram rate limits
        time.sleep(2)
    _write_manifest(results)
    _log(f"DONE ok={overall} → {_OUT_ROOT / 'MANIFEST.txt'}")
    return 0 if overall else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
