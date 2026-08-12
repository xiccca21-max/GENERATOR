# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import REMOTE_DIR, SERVICE, connect  # noqa: E402

FILES = [
    "openpdf_deflate.py",
    "tbank_sbp_stealth.py",
    "tbank_phone_stealth.py",
    "sber_dynamic.py",
    "alfa_orig_mode.py",
]


def main() -> int:
    c = connect()
    sftp = c.open_sftp()
    for rel in FILES:
        sftp.put(str(ROOT / rel), f"{REMOTE_DIR}/{rel}")
        print("put", rel)
    sftp.put(str(ROOT / "deploy" / "_diag_tbank_sbp_hard.py"), "/tmp/sbp_hard.py")
    sftp.close()

    stdin, stdout, stderr = c.exec_command(
        f"systemctl restart {SERVICE} && sleep 2 && systemctl is-active {SERVICE}",
        timeout=30,
    )
    print("service:", stdout.read().decode().strip())

    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python -u /tmp/sbp_hard.py",
        timeout=300,
    )
    print(stdout.read().decode("utf-8", "replace")[-2500:])
    print(stderr.read().decode("utf-8", "replace")[-2500:])

    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python -u tools/_smoke_hard_inputs.py",
        timeout=400,
    )
    print(stdout.read().decode("utf-8", "replace"))
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
