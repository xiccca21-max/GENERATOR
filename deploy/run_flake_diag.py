# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import REMOTE_DIR, connect  # noqa: E402

REMOTE_SCRIPT = r'''
import logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("fontTools").setLevel(logging.ERROR)
from tbank_card_tbank_stealth import create_tbank_card_tbank_stealth
from alfa_sbp_stealth import create_alfa_sbp_stealth
for i in range(3):
    r = create_tbank_card_tbank_stealth({
        "amount": "8000", "sender": "Павел Волков", "receiver": "Ольга О.",
        "card": "*3269", "date_time": "сейчас", "receipt_num": "авто",
    })
    print("CARD", i, None if not r else len(r))
for i in range(3):
    r = create_alfa_sbp_stealth({
        "amount": "7000", "sender": "Роман Щеглов", "receiver": "Инна Ю.",
        "bank": "Сбербанк", "phone": "+79001234567", "date_time": "сейчас",
    })
    print("ALFA", i, None if not r else len(r))
'''


def main() -> int:
    c = connect()
    sftp = c.open_sftp()
    with sftp.file("/tmp/flake.py", "w") as f:
        f.write(REMOTE_SCRIPT)
    sftp.close()
    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python -u /tmp/flake.py",
        timeout=300,
    )
    print(stdout.read().decode("utf-8", "replace"))
    print(stderr.read().decode("utf-8", "replace")[-3000:])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
