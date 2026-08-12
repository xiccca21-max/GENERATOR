"""Java Deflater(6,false) — как Oracle BI Publisher / PROTON CanonicalDeflater.

Keep a persistent --serve worker so Alfa flate nudges are not JVM-per-call.
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import struct
import subprocess
import threading
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CLASS = "CanonicalDeflater"
_LEVEL = 6

_worker_lock = threading.RLock()
_worker: Optional[subprocess.Popen] = None


def _checker_cp() -> Optional[Path]:
    candidates = [
        Path(__file__).resolve().parent / "tools" / "java_deflater",
        Path(__file__).resolve().parent / "java_deflater",
        Path(r"C:\Users\fanis\OneDrive\Desktop\pdf-checker-bot\tools\java_deflater"),
    ]
    for path in candidates:
        if (path / f"{_CLASS}.class").is_file():
            return path
    return None


@lru_cache(maxsize=1)
def _java_bin() -> Optional[str]:
    candidates: list[str] = []
    env = os.environ.get("JAVA_HOME")
    if env:
        candidates.append(str(Path(env) / "bin" / "java.exe"))
        candidates.append(str(Path(env) / "bin" / "java"))
    ms = Path(r"C:\Program Files\Microsoft")
    if ms.is_dir():
        for jdk in sorted(ms.glob("jdk-*"), reverse=True):
            candidates.append(str(jdk / "bin" / "java.exe"))
            candidates.append(str(jdk / "bin" / "java"))
    for base in (
        Path(r"C:\Program Files\Eclipse Adoptium"),
        Path(r"C:\Program Files\Java"),
    ):
        if not base.is_dir():
            continue
        for jdk in sorted(base.glob("jdk*"), reverse=True):
            candidates.append(str(jdk / "bin" / "java.exe"))
    which = shutil.which("java")
    if which:
        candidates.append(which)

    cp = _checker_cp()
    for cand in candidates:
        if not cand or not Path(cand).exists():
            continue
        if cp is None:
            return cand
        try:
            smoke = subprocess.run(
                [cand, "-cp", str(cp), _CLASS],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        combined = (smoke.stderr or b"") + (smoke.stdout or b"")
        if b"UnsupportedClassVersionError" in combined:
            continue
        if smoke.returncode in (0, 1, 2) or b"usage:" in combined.lower():
            return cand
    return None


def _kill_worker_unlocked() -> None:
    global _worker
    proc = _worker
    _worker = None
    if not proc:
        return
    try:
        if proc.stdin and proc.poll() is None:
            proc.stdin.write(struct.pack(">I", 0))
            proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _stop_worker() -> None:
    with _worker_lock:
        _kill_worker_unlocked()


atexit.register(_stop_worker)


def _ensure_worker_unlocked() -> Optional[subprocess.Popen]:
    global _worker
    java = _java_bin()
    cp = _checker_cp()
    if not java or not cp:
        return None
    if _worker is not None and _worker.poll() is None:
        return _worker
    _kill_worker_unlocked()
    try:
        _worker = subprocess.Popen(
            [java, "-cp", str(cp), _CLASS, "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as exc:
        logger.warning("Alfa java worker start failed: %s", exc)
        _worker = None
        return None
    return _worker


def _deflate_via_worker(data: bytes, level: int) -> Optional[bytes]:
    with _worker_lock:
        proc = _ensure_worker_unlocked()
        if proc is None or proc.stdin is None or proc.stdout is None:
            return None
        try:
            proc.stdin.write(struct.pack(">I", len(data)))
            proc.stdin.write(bytes([level & 0xFF]))
            proc.stdin.write(data)
            proc.stdin.flush()
            hdr = proc.stdout.read(4)
            if len(hdr) != 4:
                raise OSError("worker closed")
            (out_len,) = struct.unpack(">I", hdr)
            if out_len <= 0 or out_len > 64 * 1024 * 1024:
                raise OSError(f"bad worker len {out_len}")
            out = proc.stdout.read(out_len)
            if len(out) != out_len:
                raise OSError("short worker read")
            return out
        except Exception as exc:
            logger.warning("Alfa java worker fail: %s — restart", exc)
            _kill_worker_unlocked()
            return None


def _deflate_oneshot(data: bytes, level: int) -> Optional[bytes]:
    java = _java_bin()
    cp = _checker_cp()
    if not java or not cp:
        logger.warning("Alfa java_deflate unavailable (java=%s cp=%s)", java, cp)
        return None
    try:
        proc = subprocess.run(
            [java, "-cp", str(cp), _CLASS, "--level", str(level), "--stdin"],
            input=data,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Alfa java_deflate failed: %s", exc)
        return None
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"")[:200]
        logger.warning("Alfa java_deflate rc=%s err=%s", proc.returncode, err)
        return None
    return proc.stdout


def java_deflate(data: bytes, level: int = _LEVEL) -> Optional[bytes]:
    """zlib-payload = Java ``new Deflater(level, false)``; None если JDK недоступен."""
    if data is None:
        return None
    lvl = level if 1 <= int(level) <= 9 else _LEVEL
    out = _deflate_via_worker(data, lvl)
    if out is not None:
        return out
    return _deflate_oneshot(data, lvl)
