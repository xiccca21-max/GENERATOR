# -*- coding: utf-8 -*-
"""Emergency rollback: upload generator backup 24.07.26 → VPS and restart."""
from __future__ import annotations

import posixpath
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))

from deploy import SERVICE, REMOTE_DIR, connect  # noqa: E402

BAK = Path(r"C:\Users\fanis\OneDrive\Desktop\ВАЖНО!!!\ЗАПАСКА генератора 24.07.26")


def main() -> int:
    if not BAK.is_dir():
        print("backup missing", BAK)
        return 1
    files = [p for p in sorted(BAK.glob("*.py")) if p.name != "config_bot.py"]
    print(f"upload {len(files)} files from {BAK}")
    c = connect()
    sftp = c.open_sftp()
    for p in files:
        remote = posixpath.join(REMOTE_DIR, p.name)
        print(f"-> {p.name}")
        sftp.put(str(p), remote)

    smoke_path = "/tmp/rollback_smoke.py"
    smoke_py = (
        "# -*- coding: utf-8 -*-\n"
        "import logging\n"
        "logging.disable(logging.CRITICAL)\n"
        "from tbank_sbp_stealth import create_tbank_sbp_stealth\n"
        "from sber_sbp_stealth import create_sber_sbp_stealth\n"
        "tb = create_tbank_sbp_stealth({\n"
        '  "amount": "5000",\n'
        '  "sender": "Игорь Соколов",\n'
        '  "receiver": "Анна А.",\n'
        '  "bank": "Сбербанк",\n'
        '  "phone": "+7 999 123-45-67",\n'
        '  "date_time": "сейчас",\n'
        '  "receipt_num": "авто",\n'
        '  "sbp_id": "авто",\n'
        "})\n"
        'print("TBANK", None if not tb else len(tb))\n'
        "sb = create_sber_sbp_stealth({\n"
        '  "amount": "98800",\n'
        '  "sender_name": "Мария Александровна К.",\n'
        '  "receiver_name": "Мария Александровна П.",\n'
        '  "phone": "+7 985 058-40-26",\n'
        '  "bank_name": "Сбербанк",\n'
        '  "date_time": "сейчас",\n'
        "})\n"
        'print("SBER", None if not sb else len(sb))\n'
    )
    with sftp.file(smoke_path, "w") as f:
        f.write(smoke_py)
    sftp.close()

    stdin, stdout, stderr = c.exec_command(
        f"systemctl restart {SERVICE} && sleep 2 && systemctl is-active {SERVICE}"
    )
    print("service:", stdout.read().decode().strip())
    err = stderr.read().decode()
    if err:
        print("restart err:", err[:400])

    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && python3 {smoke_path}", timeout=180
    )
    print(stdout.read().decode("utf-8", "replace"))
    e = stderr.read().decode("utf-8", "replace")
    if e:
        print("STDERR", e[-1500:])
    c.close()
    print("DONE ROLLBACK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
