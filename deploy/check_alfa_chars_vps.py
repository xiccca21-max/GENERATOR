# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import REMOTE_DIR, connect  # noqa: E402

SCRIPT = r'''
from alfa_orig_mode import union_available_chars
from alfa_corpus import canonical_paths
import alfa_glyph_library as agl
agl.ensure_library()
chars = set(agl.available_chars()) | union_available_chars(canonical_paths("sbp"))
need = "Роман ЩегловИнна Ю.Сбербанк"
miss = [c for c in need if c not in chars and c not in " ."]
print("chars", len(chars), "miss", miss)
print("donors", len(list(canonical_paths("sbp"))))
'''


def main() -> int:
    c = connect()
    sftp = c.open_sftp()
    with sftp.file("/tmp/alfa_chars.py", "w") as f:
        f.write(SCRIPT)
    sftp.close()
    stdin, stdout, stderr = c.exec_command(
        f"cd {REMOTE_DIR} && PYTHONPATH={REMOTE_DIR} .venv/bin/python /tmp/alfa_chars.py",
        timeout=60,
    )
    print(stdout.read().decode("utf-8", "replace"))
    print(stderr.read().decode("utf-8", "replace")[-1500:])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
