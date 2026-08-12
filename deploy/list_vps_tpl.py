# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import connect, REMOTE_DIR  # noqa: E402

def main():
    c = connect()
    stdin, stdout, stderr = c.exec_command(
        f"ls {REMOTE_DIR}/templates | head -50; echo ---; "
        f"ls {REMOTE_DIR}/templates/Alfa* {REMOTE_DIR}/templates/alfa* 2>/dev/null; "
        f"ls -d {REMOTE_DIR}/templates/*corpus* {REMOTE_DIR}/corpus 2>/dev/null"
    )
    print(stdout.read().decode())
    c.close()

if __name__ == "__main__":
    main()
