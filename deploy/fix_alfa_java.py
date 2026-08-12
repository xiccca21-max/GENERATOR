# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import REMOTE_DIR, SERVICE, connect  # noqa: E402


def main() -> int:
    c = connect()
    sftp = c.open_sftp()
    for d in ("/opt/receipt-bot/tools", "/opt/receipt-bot/tools/java_deflater"):
        try:
            sftp.mkdir(d)
        except OSError:
            pass
    local = ROOT / "tools" / "java_deflater"
    for p in local.iterdir():
        sftp.put(str(p), f"/opt/receipt-bot/tools/java_deflater/{p.name}")
        print("put", p.name)
    sftp.put(str(ROOT / "alfa_java_deflate.py"), f"{REMOTE_DIR}/alfa_java_deflate.py")
    sftp.put(str(ROOT / "deploy" / "_diag_fail2.py"), "/tmp/diag2.py")
    sftp.close()

    stdin, stdout, stderr = c.exec_command(
        "cd /opt/receipt-bot/tools/java_deflater && javac CanonicalDeflater.java && "
        "cd /opt/receipt-bot && PYTHONPATH=/opt/receipt-bot .venv/bin/python -c "
        "\"from alfa_java_deflate import java_deflate; d=b'x'*200; h=java_deflate(d); print(h[:2].hex() if h else None)\"",
        timeout=60,
    )
    print(stdout.read().decode())
    print(stderr.read().decode()[-800:])

    stdin, stdout, stderr = c.exec_command(
        f"systemctl restart {SERVICE} && sleep 2 && systemctl is-active {SERVICE}"
    )
    print("service:", stdout.read().decode().strip())

    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python /tmp/diag2.py",
        timeout=180,
    )
    print(stdout.read().decode("utf-8", "replace"))
    print(stderr.read().decode("utf-8", "replace")[-2000:])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
