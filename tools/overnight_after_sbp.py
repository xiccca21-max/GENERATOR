# -*- coding: utf-8 -*-
"""After tbank_sbp strict50 both gates, continue other live channels overnight."""
import json, os, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
CHANS = [
    "tbank_card_sber","tbank_card_tbank","tbank_phone","tbank_nocomm",
    "sber_sbp","sber_phone","alfa_sbp","alfa_card","alfa_phone",
]
def wait_sbp(timeout_h=7.5):
    deadline = time.time() + timeout_h*3600
    fx = ROOT/"output/fraudex_full_run/strict50_fraudex_summary.json"
    op = ROOT/"output/onlypdf_full_run/strict50_summary.json"
    while time.time() < deadline:
        ok_fx = ok_op = False
        for p, gate in ((fx,"fx"),(op,"op")):
            if p.exists():
                try:
                    d=json.loads(p.read_text(encoding="utf-8"))
                    ch=next((c for c in d.get("channels",[]) if c.get("channel")=="tbank_sbp"), None)
                    if ch and ch.get("ok"):
                        if gate=="fx": ok_fx=True
                        else: ok_op=True
                except Exception:
                    pass
        # also check live processes still running
        if ok_fx and ok_op:
            return True
        time.sleep(60)
    return False
def main():
    print("waiting tbank_sbp dual 50/50...", flush=True)
    wait_sbp()
    env={**os.environ, "PYTHONIOENCODING":"utf-8","HTTP_PROXY":"http://127.0.0.1:12334","HTTPS_PROXY":"http://127.0.0.1:12334","ALL_PROXY":"http://127.0.0.1:12334"}
    only=",".join(CHANS)
    for script, tag in (("run_strict50_fraudex.py","fx"), ("run_strict50_all.py","op")):
        print(f"START rest {tag}", flush=True)
        subprocess.call([PY,"-u",str(ROOT/"tools"/script),"--only",only,"--force","--retries","8"], cwd=str(ROOT), env=env)
    print("DONE overnight rest", flush=True)
if __name__ == "__main__":
    main()
