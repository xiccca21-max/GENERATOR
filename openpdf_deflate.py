"""OpenPDF flate compression — byte-compatible with JasperReports / T-Bank PDFs."""
from __future__ import annotations

import atexit
import logging
import os
import re
import struct
import subprocess
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
_JAR = os.path.join(_DIR, "openpdf-1.3.30.jar")
_JAVA_SRC = os.path.join(_DIR, "OpenPdfDeflate.java")
_JAVA_CLASS = os.path.join(_DIR, "OpenPdfDeflate.class")

_java_exe: Optional[str] = None
_ready: Optional[bool] = None

# Persistent JVM (--serve) — без spawn на каждый pad-probe.
_worker_lock = threading.RLock()
_worker: Optional[subprocess.Popen] = None
_DEFLATE_BUDGET = threading.local()
_MAX_DEFLATE_PROBES = 220  # oversize shrink needs more probes for ±1..15B Length hits


def _find_java() -> Optional[str]:
    global _java_exe
    if _java_exe is not None:
        return _java_exe or None
    candidates = []
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(os.path.join(java_home, "bin", "java.exe"))
        candidates.append(os.path.join(java_home, "bin", "java"))
    for base in (
        r"C:\Program Files\Eclipse Adoptium",
        r"C:\Program Files\Java",
        r"C:\Program Files\Microsoft",
    ):
        if os.path.isdir(base):
            for root, _dirs, files in os.walk(base):
                if "java.exe" in files and "javac.exe" in {
                    f for f in os.listdir(root) if f.endswith(".exe")
                }:
                    candidates.append(os.path.join(root, "java.exe"))
                if root.count(os.sep) - base.count(os.sep) > 3:
                    break
    for path in ("java", "java.exe"):
        candidates.append(path)
    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            r = subprocess.run(
                [cand, "-version"],
                capture_output=True,
                timeout=8,
            )
            if r.returncode == 0:
                _java_exe = cand
                return cand
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
    _java_exe = ""
    return None


def _ensure_helper() -> bool:
    global _ready
    if _ready is not None:
        return _ready
    if not os.path.isfile(_JAR):
        logger.warning("openpdf jar missing: %s", _JAR)
        _ready = False
        return False
    java = _find_java()
    if not java:
        logger.warning("java runtime not found for OpenPDF deflate")
        _ready = False
        return False
    need_compile = not os.path.isfile(_JAVA_CLASS) or (
        os.path.isfile(_JAVA_SRC)
        and os.path.getmtime(_JAVA_SRC) > os.path.getmtime(_JAVA_CLASS)
    )
    if need_compile:
        javac_candidates = []
        jdir = os.path.dirname(java) if java not in ("java", "java.exe") else ""
        if jdir:
            javac_candidates.extend(
                [
                    os.path.join(jdir, "javac.exe"),
                    os.path.join(jdir, "javac"),
                ]
            )
        javac_candidates.extend(["javac", "javac.exe"])
        javac = None
        for cand in javac_candidates:
            if not cand:
                continue
            if cand in ("javac", "javac.exe"):
                try:
                    r = subprocess.run(
                        [cand, "-version"], capture_output=True, timeout=8
                    )
                    if r.returncode == 0:
                        javac = cand
                        break
                except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                    continue
            elif os.path.isfile(cand):
                javac = cand
                break
        if javac is None:
            if os.path.isfile(_JAVA_CLASS):
                logger.warning(
                    "javac not found — using existing OpenPdfDeflate.class"
                )
            else:
                logger.warning("javac not found next to %s", java)
                _ready = False
                return False
        else:
            try:
                subprocess.run(
                    [javac, "-cp", _JAR, _JAVA_SRC],
                    cwd=_DIR,
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as e:
                if os.path.isfile(_JAVA_CLASS):
                    logger.warning(
                        "OpenPdfDeflate compile failed (%s) — using existing .class",
                        e,
                    )
                else:
                    logger.warning("OpenPdfDeflate compile failed: %s", e)
                    _ready = False
                    return False
    _ready = True
    return True


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
    if not _ensure_helper():
        return None
    java = _find_java()
    if not java:
        return None
    if _worker is not None and _worker.poll() is None:
        return _worker
    _kill_worker_unlocked()
    try:
        _worker = subprocess.Popen(
            [java, "-cp", f"{_DIR}{os.pathsep}{_JAR}", "OpenPdfDeflate", "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as exc:
        logger.warning("OpenPDF worker start failed: %s", exc)
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
            logger.warning("OpenPDF worker fail: %s — restart", exc)
            _kill_worker_unlocked()
            return None


def _deflate_oneshot(data: bytes, level: int) -> Optional[bytes]:
    java = _find_java()
    if not java:
        return None
    payload = struct.pack(">I", len(data)) + data
    try:
        r = subprocess.run(
            [java, "-cp", f"{_DIR}{os.pathsep}{_JAR}", "OpenPdfDeflate", str(level)],
            input=payload,
            capture_output=True,
            timeout=30,
            check=True,
        )
        return r.stdout
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as e:
        err = getattr(e, "stderr", b"") or b""
        if isinstance(e, subprocess.CalledProcessError):
            err = e.stderr or b""
        logger.warning("OpenPDF deflate failed: %s %s", e, err[:200])
        return None


def _budget_ok() -> bool:
    left = getattr(_DEFLATE_BUDGET, "left", None)
    if left is None:
        return True
    if left <= 0:
        return False
    _DEFLATE_BUDGET.left = left - 1
    return True


def openpdf_deflate(data: bytes, level: int = 6) -> Optional[bytes]:
    """Compress like OpenPDF 1.3.30 / JasperReports (not stdlib zlib)."""
    if not _budget_ok():
        return None
    if not _ensure_helper():
        return None
    lvl = level if 1 <= int(level) <= 9 else 6
    out = _deflate_via_worker(data, lvl)
    if out is not None:
        return out
    # Worker died mid-batch (common under parallel probes) — one clean restart.
    with _worker_lock:
        _kill_worker_unlocked()
    out = _deflate_via_worker(data, lvl)
    if out is not None:
        return out
    return _deflate_oneshot(data, lvl)


def compress_like_jasper(data: bytes, level: int = 6) -> bytes:
    """OpenPDF only — Python zlib ловится чекерами как TBANK_DEFLATE_PROFILE_MISMATCH."""
    for attempt in range(3):
        comp = openpdf_deflate(data, level)
        if comp is not None:
            return comp
        logger.warning("OpenPDF deflate miss attempt %d", attempt + 1)
    raise RuntimeError("OpenPDF deflate unavailable — refuse zlib fallback")


def _split_et(stream: bytes) -> tuple[bytes, bytes]:
    last_et = stream.rfind(b"ET")
    if last_et < 0:
        return stream, b""
    return stream[: last_et + 2], stream[last_et + 2 :]


def _insert_pts(body: bytes) -> list[int]:
    """Safe whitespace insert points for OpenPDF size-fitting.

    OnlyPDF burns padding that lands *after* a text-block ``ET`` (between
    ``ET`` and the following ``1 0 0 1 cm`` / image ops). Prefer the Jasper
    text-end slot ``0 g\\n`` + spaces + ``ET``. Handles both LF and CRLF.
    """
    pts: list[int] = []
    # Best: right before ET that closes a painted text block.
    for m in re.finditer(rb"0 g\r?\n(?=ET)", body):
        pts.append(m.end())
    for m in re.finditer(rb"\r?\n(?=ET\b)", body):
        pts.append(m.start() + (2 if body[m.start() : m.start() + 2] == b"\r\n" else 1))
    # Secondary: before BT / Q / q (still inside or between blocks).
    for m in re.finditer(rb"\r?\n(?=(?:BT|Q|q)\b)", body):
        pts.append(m.start() + (2 if body[m.start() : m.start() + 2] == b"\r\n" else 1))

    safe: list[int] = []
    for pt in pts:
        if pt < 0 or pt > len(body):
            continue
        # Never pad right after the page `q\n` header — SafeCheck «структура».
        if pt <= 4 or body[:pt].strip() in (b"", b"q"):
            continue
        prefix = body[:pt]
        last_et = prefix.rfind(b"ET")
        last_bt = prefix.rfind(b"BT")
        # Skip points that sit after an ET with no newer BT (post-text zone).
        if last_et >= 0 and last_et > last_bt:
            continue
        # Never pad immediately before post-ET "1 0 0 1" graphics.
        if body[pt : pt + 7] == b"1 0 0 1":
            continue
        safe.append(pt)

    if not safe:
        last_et = body.rfind(b"ET")
        if last_et > 0:
            safe = [last_et]
        else:
            safe = [max(0, len(body) - 1)]
    # Prefer late insert points (less visual risk); keep a couple early ones.
    ordered = list(dict.fromkeys(list(reversed(safe))[:8] + safe[:2]))
    return ordered


def _pad_after_et_burned(stream: bytes) -> bool:
    """True if whitespace was stuffed after text ET (OnlyPDF forgery fingerprint)."""
    # receipt-ish: 0 g\nET\n + spaces + graphics cm
    return re.search(rb"0 g\nET\n[ \t]{8,}1 0 0 1", stream) is not None


def _mask_tj_literals(stream: bytes) -> bytes:
    """Replace (...)Tj / <...>Tj string bodies with '.' for safe operator scans."""
    out = bytearray(stream)
    i = 0
    n = len(stream)
    while i < n:
        if stream[i] == 0x28:  # (
            j = i + 1
            esc = False
            while j < n:
                c = stream[j]
                if esc:
                    esc = False
                    j += 1
                    continue
                if c == 0x5C:
                    esc = True
                    j += 1
                    continue
                if c == 0x29:
                    break
                j += 1
            out[i : j + 1] = b"." * (j + 1 - i)
            i = j + 1
            continue
        if stream[i] == 0x3C:  # <
            j = stream.find(b">", i + 1)
            if j < 0:
                break
            out[i : j + 1] = b"." * (j + 1 - i)
            i = j + 1
            continue
        i += 1
    return bytes(out)


def _operator_space_drop_variants(stream: bytes, limit: int = 80):
    """Drop single spaces outside PDF string literals (shrink flate 1–3B).

    Never glue an operator to the next number (``q 175`` → ``q175`` breaks the
    Jasper stamp ``cm`` / ``Do`` and the signature disappears on face).
    """
    masked = _mask_tj_literals(stream)
    n = 0
    for i in range(len(stream) - 1, 0, -1):
        if stream[i] != 0x20 or masked[i] != 0x20:
            continue
        left = stream[i - 1 : i]
        right = stream[i + 1 : i + 2] if i + 1 < len(stream) else b""
        # Separator between tokens — only drop when adjacent whitespace already
        # keeps the token boundary (``q \\n`` / ``  ``), never ``q``+digit.
        if right and right in b"0123456789.+-/" and left not in b" \n\t\r":
            continue
        if left not in b" \n\t\r" and right not in b" \n\t\r":
            continue
        yield stream[:i] + stream[i + 1 :]
        n += 1
        if n >= limit:
            return


def _shrink_variants(body: bytes, tail: bytes, limit: int = 48):
    """Collapse redundant whitespace to shrink flate output."""
    seen = set()
    cur = body
    n = 0
    for a, b in (
        (b"  ", b" "),
        (b"\n\n", b"\n"),
        (b" \n", b"\n"),
        (b"\n ", b"\n"),
        (b"   ", b" "),
        (b"\n\n\n", b"\n"),
        (b"\r\n", b"\n"),
    ):
        local = cur
        for _ in range(16):
            if a not in local:
                break
            local = local.replace(a, b, 1)
            cand = local + tail
            if cand not in seen:
                seen.add(cand)
                yield cand
                n += 1
                if n >= limit:
                    return
        cur = local


def _pad_at_level(
    body: bytes, tail: bytes, pts: list[int], target_size: int, lvl: int
) -> Optional[bytes]:
    """Grow whitespace until OpenPDF(level)=target_size.

    Cap space-run length: corpus CS never has 8+ consecutive spaces.
    Prefer small fills / newlines over SafeCheck-burned space pads.
    Never create `` ET`` (space immediately before ET) — Proton
    ``TBANK_CONTENT_ET_WHITESPACE_ANOMALY``.
    """
    max_spaces = 7

    def _before_et(pt: int) -> bool:
        return body[pt : pt + 2] == b"ET"

    def _ok(cand: bytes) -> bool:
        # Space or blank line immediately before ET → Proton ET_WHITESPACE_ANOMALY.
        # Must catch LF and CRLF (``\r\n`` * N before ET slipped past ``\n\nET``).
        if re.search(rb"[ \t]ET\b", cand):
            return False
        if re.search(rb"(?:\r?\n)[ \t]*(?:\r?\n)+[ \t]*ET\b", cand):
            return False
        if re.search(rb"[ ]{8,}", cand):
            return False
        return True

    for pt in pts[:12]:
        # Never grow whitespace immediately before ET: extra ``\n`` / ``\r\n``
        # → blank line → TBANK_CONTENT_ET_WHITESPACE_ANOMALY; spaces → « ET».
        if _before_et(pt):
            continue
        unit = b" "
        lo, hi = 0, max_spaces
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = body[:pt] + (unit * mid) + body[pt:] + tail
            if not _ok(cand):
                hi = mid - 1
                continue
            comp = openpdf_deflate(cand, lvl)
            if comp is None:
                break
            n = len(comp)
            if n == target_size:
                return cand
            if n < target_size:
                lo = mid + 1
            else:
                hi = mid - 1
        for k in range(0, max_spaces + 1):
            cand = body[:pt] + (unit * k) + body[pt:] + tail
            if not _ok(cand):
                continue
            comp = openpdf_deflate(cand, lvl)
            if comp is None:
                break
            if len(comp) == target_size:
                return cand
        # Newlines only away from ET (between operators), never blank-before-ET.
        for fill in (b"\n", b"\r\n", b"\n ", b"\n \n"):
            for rep in range(1, 8):
                cand = body[:pt] + (fill * rep) + body[pt:] + tail
                if not _ok(cand):
                    continue
                comp = openpdf_deflate(cand, lvl)
                if comp is None:
                    break
                if len(comp) == target_size:
                    return cand
            else:
                continue
            break
        if getattr(_DEFLATE_BUDGET, "left", None) is not None and _DEFLATE_BUDGET.left <= 0:
            _DEFLATE_BUDGET.left = 120
    return None


def _space_drop_variants(body: bytes, tail: bytes, limit: int = 96):
    """Drop one redundant space from double-space / trailing-line runs."""
    n = 0
    seen = set()
    for i in range(len(body) - 1, 0, -1):
        if body[i : i + 1] != b" ":
            continue
        left = body[i - 1 : i]
        right = body[i + 1 : i + 2] if i + 1 < len(body) else b""
        if left != b" " and right != b"\n" and right != b" ":
            continue
        cand = body[:i] + body[i + 1 :] + tail
        if cand in seen:
            continue
        seen.add(cand)
        yield cand
        n += 1
        if n >= limit:
            return


def fit_decoded_to_openpdf_size(
    stream: bytes, target_size: int, level: int = 6
) -> Optional[bytes]:
    """Decoded content that OpenPDF flate-compresses to exactly target_size.

    Stick to the requested zlib level (Jasper/T-Bank = 6) so the deflate
    profile matches — do not swap to level 8/9 just to hit a byte count.
    Whitespace only before final ET — no %-comments after ET.
    """
    prev = getattr(_DEFLATE_BUDGET, "left", None)
    # Near-miss oversize needs many probes; give a dedicated budget per call.
    _DEFLATE_BUDGET.left = max(_MAX_DEFLATE_PROBES, 400)
    try:
        body, tail = _split_et(stream)
        pts = _insert_pts(body)
        lvl = level

        base = openpdf_deflate(stream, lvl)
        if base is not None and len(base) == target_size:
            return stream
        if base is not None and len(base) < target_size:
            hit = _pad_at_level(body, tail, pts, target_size, lvl)
            if hit is not None and not _pad_after_et_burned(hit):
                return hit
            return None

        overshoot = (len(base) - target_size) if base is not None else 99
        # Oversized: shrink whitespace, then pad if undershoot.
        # Do NOT drop operator spaces (risk: `q 28` → `q28` → OnlyPDF FAKE).
        tried = [stream]
        tried.extend(_shrink_variants(body, tail, limit=96 if overshoot <= 16 else 48))
        tried.extend(_space_drop_variants(body, tail, limit=128 if overshoot <= 16 else 64))
        extras = []
        for c in tried[1:24]:
            cb, ct = _split_et(c)
            extras.extend(_space_drop_variants(cb, ct, limit=24))
        # Also try inserting a single newline at many pts then shrink — can undershoot.
        for pt in pts[:10]:
            for fill in (b"\n", b"\n\n"):
                tried.append(body[:pt] + fill + body[pt:] + tail)
        # Near-miss overshoot: only whitespace shrink — never drop newlines that
        # glue PDF operators (Td\\nET → TdET → Fraudex structure / MuPDF error).
        for cand in tried + extras:
            if getattr(_DEFLATE_BUDGET, "left", 1) <= 0:
                _DEFLATE_BUDGET.left = 80  # top-up once when close
            if re.search(rb"(?:Td|Tm|Tj|BT|ET|cm|rg|RG|Tf)(?:Td|Tm|Tj|BT|ET|cm)", cand):
                continue
            comp = openpdf_deflate(cand, lvl)
            if comp is None:
                continue
            if len(comp) == target_size:
                if _pad_after_et_burned(cand):
                    continue
                return cand
            if len(comp) < target_size:
                cb, ct = _split_et(cand)
                hit = _pad_at_level(cb, ct, _insert_pts(cb), target_size, lvl)
                if hit is not None and not _pad_after_et_burned(hit):
                    return hit
        return None
    finally:
        _DEFLATE_BUDGET.left = prev


def compress_to_size(data: bytes, target_size: int, level: int = 6) -> Optional[bytes]:
    """Exact OpenPDF/Jasper size at the given zlib level (default 6)."""
    fitted = fit_decoded_to_openpdf_size(data, target_size, level=level)
    if fitted is None:
        return None
    if _pad_after_et_burned(fitted):
        logger.warning("OpenPDF pad landed after ET — reject size fit")
        return None
    # Fingerprint checks only outside PDF string literals (Tj bodies may pad with spaces).
    ops = _mask_tj_literals(fitted)
    if re.search(rb"q\n[ ]{8,}", ops) or re.search(rb"[ ]{8,}(?=\n?ET\b)", ops):
        logger.warning("OpenPDF pad long space-run — reject size fit")
        return None
    if re.search(rb"[ \t]ET\b", ops) or re.search(
        rb"(?:\r?\n)[ \t]*(?:\r?\n)+[ \t]*ET\b", ops
    ):
        logger.warning("OpenPDF pad space/blank-before-ET — reject size fit")
        return None
    if re.search(rb"[ ]{16,}", ops):
        logger.warning("OpenPDF pad space-fingerprint — reject size fit")
        return None
    comp = openpdf_deflate(fitted, level)
    if comp is not None and len(comp) == target_size:
        return comp
    return None
