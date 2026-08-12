# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import REMOTE_DIR, SERVICE, connect  # noqa: E402

FILES = [
    "alfa_orig_mode.py",
    "alfa_java_deflate.py",
    "openpdf_deflate.py",
    "tbank_phone_stealth.py",
    "sber_dynamic.py",
    "tools/_smoke_all_channels.py",
    "tools/_smoke_hard_inputs.py",
]


def main() -> int:
    c = connect()
    sftp = c.open_sftp()
    for rel in FILES:
        local = ROOT / rel
        remote = f"{REMOTE_DIR}/{rel.replace(chr(92), '/')}"
        parent = "/".join(remote.split("/")[:-1])
        cur = ""
        for p in parent.split("/"):
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
    if jd.is_dir():
        for p in jd.iterdir():
            sftp.put(str(p), f"{REMOTE_DIR}/tools/java_deflater/{p.name}")
    sftp.close()

    stdin, stdout, stderr = c.exec_command(
        f"systemctl restart {SERVICE} && sleep 2 && systemctl is-active {SERVICE}",
        timeout=30,
    )
    print("service:", stdout.read().decode().strip())

    for script in ("tools/_smoke_all_channels.py", "tools/_smoke_hard_inputs.py"):
        print("===", script, "===")
        stdin, stdout, stderr = c.exec_command(
            f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python -u {script}",
            timeout=400,
        )
        print(stdout.read().decode("utf-8", "replace"))
        err = stderr.read().decode("utf-8", "replace")
        if err.strip():
            print("stderr:", err[-600:])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
