# -*- coding: utf-8 -*-
"""Deploy fixed generators to VPS and smoke all live channels."""
from __future__ import annotations

import posixpath
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from deploy import REMOTE_DIR, SERVICE, connect  # noqa: E402

FILES = [
    "bot.py",
    "tbank_sbp_stealth.py",
    "tbank_phone_stealth.py",
    "tbank_card_tbank_stealth.py",
    "tbank_nocomm_stealth.py",
    "tbank_stealth_v3.py",
    "tbank_dynamic.py",
    "tbank_orig_mode.py",
    "tbank_channel_common.py",
    "tbank_corpus.py",
    "tbank_donor_fit.py",
    "sber_sbp_stealth.py",
    "sber_phone_stealth.py",
    "sber_dynamic.py",
    "sber_glyph_library.py",
    "sber_stealth_v3.py",
    "alfa_sbp_stealth.py",
    "alfa_card_stealth.py",
    "alfa_phone_stealth.py",
    "alfa_font_extend.py",
    "alfa_glyph_library.py",
    "alfa_orig_mode.py",
    "alfa_phone_orig_mode.py",
    "openpdf_deflate.py",
    "time_msk.py",
]

SMOKE = r'''# -*- coding: utf-8 -*-
import logging
logging.disable(logging.CRITICAL)

def run(tag, fn, data):
    try:
        pdf = fn(data)
    except Exception as e:
        print(f"FAIL {tag} EXC {type(e).__name__}: {e}")
        return 1
    if not pdf:
        print(f"FAIL {tag} None")
        return 1
    print(f"OK   {tag} {len(pdf)}")
    return 0

bad = 0
from tbank_sbp_stealth import create_tbank_sbp_stealth
from tbank_phone_stealth import create_tbank_phone_stealth
from tbank_card_tbank_stealth import create_tbank_card_tbank_stealth
from tbank_nocomm_stealth import create_tbank_nocomm_stealth
from tbank_stealth_v3 import create_tbank_stealth
from sber_sbp_stealth import create_sber_sbp_stealth
from sber_phone_stealth import create_sber_phone_stealth
from alfa_sbp_stealth import create_alfa_sbp_stealth
from alfa_card_stealth import create_alfa_card_stealth
from alfa_phone_stealth import create_alfa_phone_stealth

bad += run("tbank_sbp", create_tbank_sbp_stealth, {
  "amount":"15000","sender":"Элина Юрьева","receiver":"Анна А.",
  "bank":"Сбербанк","phone":"+7 999 123-45-67","date_time":"сейчас",
  "receipt_num":"авто","sbp_id":"авто",
})
bad += run("tbank_phone", create_tbank_phone_stealth, {
  "amount":"12000","sender":"Игорь Соколов","receiver":"Мария М.",
  "phone":"+7 (916) 555-12-34","date_time":"сейчас","receipt_num":"авто",
})
bad += run("tbank_card_tbank", create_tbank_card_tbank_stealth, {
  "amount":"8000","sender":"Павел Волков","receiver":"Ольга О.",
  "card":"*3269","date_time":"сейчас","receipt_num":"авто",
})
bad += run("tbank_nocomm", create_tbank_nocomm_stealth, {
  "amount":"4500","sender":"Кирилл Громов","receiver":"Дарья Д.",
  "card":"*1122","date_time":"сейчас","receipt_num":"авто",
})
bad += run("tbank_card_sber", create_tbank_stealth, {
  "amount":"9000","sender":"Сергей Семенов","receiver":"Елена Е.",
  "bank":"Сбербанк","card":"*4455","date_time":"сейчас","receipt_num":"авто",
})
bad += run("sber_sbp", create_sber_sbp_stealth, {
  "amount":"98800","sender_name":"Мария Александровна К.",
  "receiver_name":"Мария Александровна П.","phone":"+7 985 058-40-26",
  "bank_name":"Сбербанк","date_time":"сейчас",
})
bad += run("sber_phone", create_sber_phone_stealth, {
  "amount":"5000","sender_name":"Иван Иванов","receiver_name":"Петр П.",
  "phone":"+7 900 111-22-33","date_time":"сейчас",
})
bad += run("alfa_sbp", create_alfa_sbp_stealth, {
  "amount":"7000","sender":"Роман Щеглов","receiver":"Инна И.",
  "bank":"Тинькофф","phone":"+79001234567","date_time":"сейчас",
})
bad += run("alfa_card", create_alfa_card_stealth, {
  "amount":"6000","card":"2200 00** **** 1234","date_time":"сейчас",
})
bad += run("alfa_phone", create_alfa_phone_stealth, {
  "amount":"5500","receiver":"Хабиров Р. Р.","phone":"79271234568",
  "date_time":"сейчас","account":"40817810123456780922",
  "operation_num":"авто","message":"авто",
})
print(f"DONE fail={bad}/10")
raise SystemExit(1 if bad else 0)
'''


def main() -> int:
    c = connect()
    sftp = c.open_sftp()
    for name in FILES:
        local = ROOT / name
        if not local.is_file():
            print("skip missing", name)
            continue
        remote = posixpath.join(REMOTE_DIR, name)
        print("->", name)
        sftp.put(str(local), remote)
    with sftp.file("/tmp/all_channels_smoke.py", "w") as f:
        f.write(SMOKE)
    sftp.close()

    stdin, stdout, stderr = c.exec_command(
        f"systemctl restart {SERVICE} && sleep 2 && systemctl is-active {SERVICE}"
    )
    print("service:", stdout.read().decode().strip())
    print(stderr.read().decode()[:300])

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
