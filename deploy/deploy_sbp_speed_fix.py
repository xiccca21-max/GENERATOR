# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import REMOTE_DIR, SERVICE, connect  # noqa: E402

SMOKE = r'''
import time
from tbank_sbp_stealth import create_tbank_sbp_stealth
d = {
    "amount": "38820",
    "sender": "Дамир Сеничев",
    "receiver": "Михаил Р.",
    "bank": "Сбербанк",
    "phone": "+7 (909) 564-33-85",
    "date_time": "30.07.2026 18:38:27",
    "receipt_num": "1-130-223-474-293",
    "sbp_id": "авто",
}
t0 = time.time()
r = create_tbank_sbp_stealth(d)
print("RESULT", None if not r else len(r), "sec", round(time.time() - t0, 1))
'''


def main() -> int:
    c = connect()
    sftp = c.open_sftp()
    for f in ("tbank_sbp_stealth.py", "bot.py", "openpdf_deflate.py"):
        sftp.put(str(ROOT / f), f"{REMOTE_DIR}/{f}")
        print("put", f)
    with sftp.file("/tmp/user_sbp.py", "w") as fh:
        fh.write(SMOKE)
    sftp.close()
    stdin, stdout, stderr = c.exec_command(
        f"systemctl restart {SERVICE} && sleep 2 && systemctl is-active {SERVICE} && "
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python -u /tmp/user_sbp.py",
        timeout=120,
    )
    print(stdout.read().decode("utf-8", "replace"))
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        print(err[-1000:])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
