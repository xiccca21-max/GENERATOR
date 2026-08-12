# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import REMOTE_DIR, SERVICE, connect  # noqa: E402

# reuse smoke body from deploy_fix_all
import re

text = (ROOT / "deploy" / "deploy_fix_all.py").read_text(encoding="utf-8")
m = re.search(r"SMOKE = r'''(.*?)'''", text, re.S)
assert m
SMOKE = m.group(1)


def main() -> int:
    c = connect()
    sftp = c.open_sftp()
    sftp.put(str(ROOT / "deploy" / "_op_smoke.py"), "/tmp/_op_smoke.py")
    with sftp.file("/tmp/all_channels_smoke.py", "w") as f:
        f.write(SMOKE)
    sftp.close()

    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python /tmp/_op_smoke.py",
        timeout=30,
    )
    print("OP:", stdout.read().decode())
    print(stderr.read().decode()[-800:])

    stdin, stdout, stderr = c.exec_command(
        f"systemctl restart {SERVICE} && sleep 2 && systemctl is-active {SERVICE}"
    )
    print("service:", stdout.read().decode().strip())

    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python /tmp/all_channels_smoke.py",
        timeout=300,
    )
    print(stdout.read().decode("utf-8", "replace"))
    err = stderr.read().decode("utf-8", "replace")
    if err:
        print("STDERR", err[-2000:])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
