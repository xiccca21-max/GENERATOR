# -*- coding: utf-8 -*-
"""Deploy alfa_orig_mode + CanonicalDeflater, restart, full channel smoke."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import REMOTE_DIR, SERVICE, connect  # noqa: E402


def main() -> int:
    c = connect()
    sftp = c.open_sftp()
    files = [
        "alfa_orig_mode.py",
        "alfa_java_deflate.py",
        "alfa_sbp_stealth.py",
        "tools/_smoke_all_channels.py",
    ]
    for rel in files:
        local = ROOT / rel
        remote = f"{REMOTE_DIR}/{rel.replace(chr(92), '/')}"
        parent = str(Path(remote).parent).replace("\\", "/")
        # sftp.mkdir parents lightly
        parts = parent.split("/")
        cur = ""
        for p in parts:
            if not p:
                continue
            cur += "/" + p
            try:
                sftp.mkdir(cur)
            except OSError:
                pass
        sftp.put(str(local), remote)
        print("put", rel)

    jd = ROOT / "tools" / "java_deflater"
    for p in jd.iterdir():
        sftp.put(str(p), f"{REMOTE_DIR}/tools/java_deflater/{p.name}")
    sftp.close()

    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR}/tools/java_deflater && javac CanonicalDeflater.java 2>&1; "
        f"systemctl restart {SERVICE} && sleep 2 && systemctl is-active {SERVICE}",
        timeout=60,
    )
    print(stdout.read().decode())
    err = stderr.read().decode()
    if err.strip():
        print("stderr:", err[-500:])

    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python tools/_smoke_all_channels.py",
        timeout=300,
    )
    print(stdout.read().decode("utf-8", "replace"))
    print(stderr.read().decode("utf-8", "replace")[-1500:])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
