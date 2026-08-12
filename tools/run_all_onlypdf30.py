# -*- coding: utf-8 -*-
"""Run ALL live channels 30/30 with dual OnlyPDF+Proton gate.

Skips Sber card (blocked). Retries each channel until pass or max_retries.
Does not stop the suite on one channel fail — continues, then exits non-zero.

Usage:
  python tools/run_all_onlypdf30.py
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
_LOG_DIR = _ROOT / "output" / "onlypdf_full_run"
_SUMMARY = _LOG_DIR / "summary.json"
_LIVE = _LOG_DIR / "live.log"

CHANNELS = [
    ("tbank_sbp", "gen_onlypdf30.py", "чеки_30_из_30", 6),
    ("tbank_card_sber", "gen_onlypdf30_card_sber.py", "_test30_card_sber", 6),
    ("tbank_card_tbank", "gen_onlypdf30_card_tbank.py", "_test30_card_tbank", 6),
    ("tbank_phone", "gen_onlypdf30_phone.py", "_test30_phone", 6),
    ("tbank_nocomm", "gen_onlypdf30_nocomm.py", "_test30_nocomm", 6),
    ("alfa_sbp", "gen_onlypdf30_alfa_sbp.py", "_test30_alfa_sbp", 6),
    ("alfa_card", "gen_onlypdf30_alfa_card.py", "_test30_alfa_card", 6),
    ("alfa_phone", "gen_onlypdf30_alfa_phone.py", "_test30_alfa_phone", 6),
    ("sber_sbp", "gen_onlypdf30_sber_sbp.py", "_test30_sber_sbp", 6),
    ("sber_phone", "gen_onlypdf30_sber_phone.py", "_test30_sber_phone", 6),
]

# Если OnlyPDF отдаёт «ошибка сервиса» даже на оригинале Альфы — канал пропускаем.
_ALFA_CANARY = _ROOT / "templates" / "Alfa_sbp_original.pdf"
_ALFA_LABELS = frozenset({"alfa_sbp", "alfa_card", "alfa_phone"})


def _onlypdf_alive_for_alfa() -> bool:
    """True если OnlyPDF способен проверить Альфу (не SERVICE_ERROR на оригинале)."""
    if not _ALFA_CANARY.is_file():
        _log(f"WARN no alfa canary {_ALFA_CANARY.name} — Alfa channels not skipped")
        return True
    try:
        sys.path.insert(0, str(_DIR))
        from onlypdf_gate import open_onlypdf_client, onlypdf_verdict  # noqa: WPS433
        import asyncio

        async def _probe() -> str:
            client, cfg = await open_onlypdf_client()
            try:
                return await onlypdf_verdict(client, _ALFA_CANARY, cfg)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        v = asyncio.run(_probe())
        _log(f"Alfa OnlyPDF canary ({_ALFA_CANARY.name}) → {v}")
        return v != "SERVICE_ERROR"
    except Exception as exc:
        _log(f"Alfa canary probe failed: {exc}")
        return True



def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _LIVE.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def _run_one(label: str, script: str, out_name: str, attempt: int) -> int:
    script_path = _DIR / script
    out = _ROOT / out_name
    log_path = _LOG_DIR / f"{label}_a{attempt}.log"
    _log(f"START {label} attempt={attempt} → {out} (log {log_path.name})")
    cmd = [_PY, "-u", str(script_path), "-n", "30", "--out", str(out)]
    t0 = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            env={
                **dict(**{k: v for k, v in __import__("os").environ.items()}),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            },
        )
        rc = proc.wait()
    dt = round(time.time() - t0, 1)
    _log(f"END {label} attempt={attempt} rc={rc} in {dt}s")
    # Tail last lines for live visibility
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        for line in tail:
            _log(f"  | {line}")
    except Exception:
        pass
    return rc


def main() -> int:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log("=" * 60)
    _log(f"FULL Dual OnlyPDF+Proton 30/30 — {len(CHANNELS)} channels")
    _log("Skip: sber_card (blocked)")
    _log("=" * 60)

    results = []
    overall_ok = True
    alfa_ok = None  # None = not probed yet

    for label, script, out_name, max_retries in CHANNELS:
        if label in _ALFA_LABELS:
            if alfa_ok is None:
                alfa_ok = _onlypdf_alive_for_alfa()
            if not alfa_ok:
                _log(
                    f"SKIP {label}: OnlyPDF SERVICE_ERROR even on Alfa original "
                    f"(backend down — not our PDF)"
                )
                results.append(
                    {
                        "channel": label,
                        "script": script,
                        "out": out_name,
                        "ok": False,
                        "rc": 99,
                        "skipped": "onlypdf_alfa_service_error",
                    }
                )
                overall_ok = False
                continue

        ok = False
        last_rc = 1
        for attempt in range(1, max_retries + 1):
            last_rc = _run_one(label, script, out_name, attempt)
            if last_rc == 0:
                ok = True
                break
            _log(f"RETRY {label} after rc={last_rc}")
            time.sleep(5)
        results.append(
            {
                "channel": label,
                "script": script,
                "out": out_name,
                "ok": ok,
                "rc": last_rc,
            }
        )
        if not ok:
            overall_ok = False
            _log(f"FATAL channel failed: {label}")

    summary = {
        "finished": datetime.now().isoformat(),
        "ok": overall_ok,
        "gate": "onlypdf+proton",
        "channels": results,
        "passed": sum(1 for r in results if r["ok"]),
        "total": len(results),
    }
    _SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"SUMMARY {summary['passed']}/{summary['total']} → {_SUMMARY}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
