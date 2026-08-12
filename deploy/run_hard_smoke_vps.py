# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import REMOTE_DIR, connect  # noqa: E402

def main() -> int:
    c = connect()
    sftp = c.open_sftp()
    sftp.put(str(ROOT / "tools" / "_smoke_hard_inputs.py"), f"{REMOTE_DIR}/tools/_smoke_hard_inputs.py")
    sftp.close()
    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python tools/_smoke_hard_inputs.py",
        timeout=300,
    )
    print(stdout.read().decode("utf-8", "replace"))
    print(stderr.read().decode("utf-8", "replace")[-1000:])
    c.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
