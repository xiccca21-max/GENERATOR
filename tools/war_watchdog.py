"""Keep full-queue Proton war alive (no --only). Restart if dead; dedupe PIDs."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "night_dual"
LOG = OUT / "watchdog.log"
PY = sys.executable
WAR_ARGS = ["-u", "tools/night_proton_seq.py", "--force", "--war", "-n", "5", "--cycle", "16"]


def _log(msg: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def _war_pids() -> list[tuple[int, str]]:
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None
    found: list[tuple[int, str]] = []
    if psutil is not None:
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(p.info["cmdline"] or [])
            except Exception:
                continue
            if "night_proton_seq.py" in cmd:
                found.append((int(p.info["pid"]), cmd))
        return found
    # Fallback: WMIC on Windows
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
            text=True,
            errors="replace",
        )
    except Exception:
        return found
    pid = None
    cmd = ""
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("CommandLine="):
            cmd = line[len("CommandLine=") :]
        elif line.startswith("ProcessId="):
            try:
                pid = int(line.split("=", 1)[1])
            except ValueError:
                pid = None
        elif not line and pid is not None:
            if "night_proton_seq.py" in cmd:
                found.append((pid, cmd))
            pid = None
            cmd = ""
    if pid is not None and "night_proton_seq.py" in cmd:
        found.append((pid, cmd))
    return found


def _kill(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def _start_war() -> None:
    for name in ("STOP", "STOP_FAKE.json"):
        p = OUT / name
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    out = open(OUT / "queue_out.txt", "ab")
    err = open(OUT / "queue_err.txt", "ab")
    kwargs = {"cwd": str(ROOT), "stdout": out, "stderr": err}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    subprocess.Popen([PY, *WAR_ARGS], **kwargs)
    _log("started full war")


def main() -> None:
    _log("watchdog up")
    while True:
        time.sleep(120)
        procs = _war_pids()
        only = [(pid, cmd) for pid, cmd in procs if "--only" in cmd]
        full = [(pid, cmd) for pid, cmd in procs if "--only" not in cmd]
        for pid, _ in only:
            _kill(pid)
            _log(f"killed --only pid={pid}")
        if len(full) > 1:
            for pid, _ in full[1:]:
                _kill(pid)
                _log(f"dedupe killed pid={pid}")
            full = full[:1]
        if not full:
            _start_war()


if __name__ == "__main__":
    main()
