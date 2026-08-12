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
    for rel in ("alfa_corpus.py", "tbank_sbp_stealth.py"):
        sftp.put(str(ROOT / rel), f"{REMOTE_DIR}/{rel}")
        print("put", rel)

    remote_corpus = f"{REMOTE_DIR}/templates/alfa_corpus"
    try:
        sftp.mkdir(f"{REMOTE_DIR}/templates")
    except OSError:
        pass
    try:
        sftp.mkdir(remote_corpus)
    except OSError:
        pass
    local = ROOT / "templates" / "alfa_corpus"
    for p in sorted(local.glob("*.pdf")):
        sftp.put(str(p), f"{remote_corpus}/{p.name}")
        print("put corpus", p.name)

    sftp.close()
    stdin, stdout, stderr = c.exec_command(
        f"systemctl restart {SERVICE} && sleep 2 && systemctl is-active {SERVICE}",
        timeout=30,
    )
    print("service:", stdout.read().decode().strip())

    # verify donors
    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python -c "
        "\"from alfa_corpus import CORPUS_DIR, canonical_paths; "
        "print(CORPUS_DIR); print('sbp', len(canonical_paths('sbp')))\"",
        timeout=60,
    )
    print(stdout.read().decode())
    print(stderr.read().decode()[-500:])

    for script in ("tools/_smoke_all_channels.py", "tools/_smoke_hard_inputs.py"):
        print("===", script, "===")
        stdin, stdout, stderr = c.exec_command(
            f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python -u {script}",
            timeout=400,
        )
        print(stdout.read().decode("utf-8", "replace"))
        err = stderr.read().decode("utf-8", "replace")
        if err.strip():
            print("stderr:", err[-400:])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
