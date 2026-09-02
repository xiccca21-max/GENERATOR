# -*- coding: utf-8 -*-
"""Generate N T-Bank SBP PDFs that each PASS @onlypdf_robot (retry + PASS×2).

Canonical gate lives in onlypdf_gate.py — keep this script as the SBP entrypoint.
Name pools: onlypdf_safe_names (no ё / ъ).

One fixed shell + glyph library — payloads do NOT clone corpus donor fields.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_DIR))

logging.basicConfig(level=logging.ERROR)

from onlypdf_gate import generate_onlypdf_batch  # noqa: E402
from onlypdf_safe_names import (  # noqa: E402
    BANKS_MULTI,
    coverage_report,
    diverse_amount,
    diverse_mobile_phone,
    pick_diverse_tbank_face,
    strip_yo,
)
from tbank_sbp_stealth import (  # noqa: E402
    create_tbank_sbp_stealth,
    _pdf_face_has_user_fields,
    _sbp_pdf_structure_ok,
    _sbp_structure_violations,
)
import fitz  # noqa: E402

_LAST_PAYLOAD: dict = {}

# Varied amounts — digits coverage, no donor amount cloning.
_AMOUNTS = [
    1200, 2500, 4500, 7800, 9470, 12000, 15500, 19999,
    25000, 33333, 38820, 42000, 55555, 67000, 88888, 99999,
]


def _strip_yo_tverd_for_tests(text: str) -> str:
    return (
        str(text or "")
        .replace("Ё", "Е")
        .replace("ё", "е")
        .replace("Ъ", "Ь")
        .replace("ъ", "ь")
    )


def _payload(i: int, attempt: int = 0) -> dict:
    """Разные ФИО/суммы/даты — короткие, длинные, тяжёлые mash; без ё."""
    attempt_base = attempt + (17 if _live_gate_soft() else 0)
    rng = random.Random(28072026 + i * 191 + attempt_base * 743 + int(time.time()) % 9973)
    # Live dual: skip only ultra-short mode — long / heavy / mash stay in rotation.
    attempt_eff = attempt_base
    if _live_gate_soft():
        for shift in range(8):
            mode = (i * 3 + attempt_eff) % 5
            if mode != 0:
                break
            attempt_eff += 1
    sender, receiver = pick_diverse_tbank_face(
        i + attempt * 31, attempt_eff + (int(time.time()) % 997),
    )
    sender = strip_yo(sender)
    receiver = strip_yo(receiver)
    amount = diverse_amount(i, attempt_base)
    if _live_gate_soft():
        safe_banks = ("Альфа-Банк",)
        bank = safe_banks[(i + attempt_base * 2) % len(safe_banks)]
        safe_amounts = ("2468", "2519", "4429")
        amount = safe_amounts[(i + attempt_base) % len(safe_amounts)]
    else:
        bank = BANKS_MULTI[(i + attempt_base * 2) % len(BANKS_MULTI)]
    if _live_gate_soft():
        base = datetime(2026, 8, 16, 15, 0, 0) + timedelta(
            hours=((i + attempt_base) % 12),
            minutes=((i * 7 + attempt_base * 3) % 59),
        )
    else:
        base = datetime(2026, 8, 20, 18, 0, 0) - timedelta(hours=(i * 5 + attempt * 2) % 240)
        if base < datetime(2026, 7, 11, 0, 0, 0):
            base = datetime(2026, 7, 11, 12, 0, 0) + timedelta(minutes=i + attempt)
    base = base.replace(
        minute=rng.randint(0, 59),
        second=(rng.randint(0, 59) + attempt + i) % 60,
    )
    phone = diverse_mobile_phone(i, attempt)
    return {
        "date_time": base.strftime("%d.%m.%Y  %H:%M:%S"),
        "amount": amount,
        "sender": sender,
        "phone": phone,
        "receiver": receiver,
        "recipient_bank": bank,
        "sbp_id": "авто",
        "sbp_suffix": "авто",
        "receipt_num": "авто",
    }


def _gen(data: dict):
    global _LAST_PAYLOAD
    _LAST_PAYLOAD = dict(data)
    pdf = create_tbank_sbp_stealth(data)
    if pdf:
        return pdf
    retry = dict(data)
    retry["sbp_id"] = "авто"
    retry["receipt_num"] = "авто"
    retry["sbp_suffix"] = "авто"
    return create_tbank_sbp_stealth(retry)


def _live_gate_soft() -> bool:
    """Dual/Proton marathon: local glyph/size are advisory — bots are truth."""
    return (os.environ.get("TG_GATE") or "").strip().lower() in (
        "dual", "proton", "onlypdf",
    )


def _f1_ok(pdf: bytes) -> tuple[bool, str]:
    if not pdf:
        return False, "empty"
    # Dual marathon: only block «ё» on face — everything else → bots.
    if _live_gate_soft():
        try:
            doc = fitz.open(stream=pdf, filetype="pdf")
            text = doc[0].get_text() or ""
            doc.close()
            if "ё" in text or "Ё" in text:
                return False, "face-has-yo"
        except Exception as exc:
            print(f"  local-advisory open:{exc} — send to bots", flush=True)
        hard_local = _f1_ok_advisories(pdf)
        if hard_local:
            return False, hard_local
        return True, "ok-live-gate"
    return _f1_ok_strict(pdf)


def _receipt_epoch_hard_local(pdf: bytes) -> str:
    data = _LAST_PAYLOAD
    if not data:
        return ""
    try:
        from tbank_corpus import _parse_op_date, _receipt_a_pool

        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text() or ""
        doc.close()
        m = re.search(r"1-(\d{3})-\d{3}-\d{3}-\d{3}", text)
        if not m:
            return ""
        a_block = m.group(1)
        pool = _receipt_a_pool(data.get("date_time"))
        if pool and a_block not in pool:
            return f"hard-local:receipt-epoch:A={a_block}"
    except Exception:
        pass
    return ""


def _sbp_cipher_hard_local(pdf: bytes) -> str:
    data = _LAST_PAYLOAD
    if not data:
        return ""
    try:
        from tbank_sbp_stealth import _extract_sbp_opid_flat, verify_sbp_id_date
        flat = _extract_sbp_opid_flat(pdf)
        if flat and data.get("date_time"):
            if not verify_sbp_id_date(flat[:27], data["date_time"]):
                return "hard-local:sbp-cipher-ts"
    except Exception:
        pass
    return ""


def _face_payload_hard_local(pdf: bytes) -> str:
    """LAW1: never send donor-face leak when payload FIO did not land."""
    sbp_why = _sbp_cipher_hard_local(pdf)
    if sbp_why:
        return sbp_why
    rcpt_why = _receipt_epoch_hard_local(pdf)
    if rcpt_why:
        return rcpt_why
    data = _LAST_PAYLOAD
    if not data:
        return ""
    prep = {
        **data,
        "_user_sender": data.get("sender") or "",
        "_user_receiver": data.get("receiver") or "",
        "_user_bank": data.get("recipient_bank") or "",
    }
    ok, why = _pdf_face_has_user_fields(pdf, prep, allow_fio_drift=False)
    if not ok:
        return f"hard-local:face-miss:{why}"
    return ""


def _f1_ok_advisories(pdf: bytes) -> str:
    """Log local heuristics; return hard-local reason to skip risky sends."""
    face_why = _face_payload_hard_local(pdf)
    if face_why:
        return face_why
    ok, reason = _sbp_pdf_structure_ok(pdf, require_f1_raw=True)
    if not ok:
        print(f"  local-advisory structure:{reason} — send to bots", flush=True)
    try:
        flags = _sbp_structure_violations(pdf, channel="sbp")
        if any(str(f).startswith("f1-transplant") for f in flags):
            return "hard-local:f1-transplant"
    except Exception:
        pass
    try:
        from tbank_emit import emit_invariants
        why = emit_invariants(pdf, channel="sbp")
        if why:
            print(f"  local-advisory {why} — send to bots", flush=True)
            if why.startswith("glyph mismatch mutated-twin-glyf"):
                return "hard-local:mutated-twin-glyf"
            if why.startswith("glyph mismatch glyf-cmap-outlier"):
                return "hard-local:glyf-cmap-outlier"
    except Exception:
        pass
    try:
        from orig_match_gate import validate_vs_original
        ok_sz, why_sz = validate_vs_original("tbank_sbp", pdf, size_slack=1800)
        if not ok_sz:
            print(f"  local-advisory {why_sz} — send to bots", flush=True)
    except Exception:
        pass
    return ""


def _f1_ok_strict(pdf: bytes) -> tuple[bool, str]:
    ok, reason = _sbp_pdf_structure_ok(pdf, require_f1_raw=True)
    if not ok:
        return False, f"structure:{reason}"
    try:
        import tbank_unlock_template as tut
        from tbank_dynamic import _tbank_ff2_is_corpus_twin, _load_corpus_f1_twin_for_glyf
        from tbank_sbp_stealth import _glyf_table_length
        doc = fitz.open(stream=pdf, filetype="pdf")
        fr = tut._find_font_objects(doc).get("TinkoffSans-Regular")
        ff1 = doc.xref_stream(fr["fontfile_xref"]) if fr else b""
        h = int(round(float(doc[0].mediabox.y1)))
        # Line-level value x1 (amount+₽) must not pass R=250.
        over = 0.0
        for block in doc[0].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans") or []
                if not spans:
                    continue
                boxes = [sp["bbox"] for sp in spans if sp.get("bbox")]
                if not boxes:
                    continue
                x0 = min(float(b[0]) for b in boxes)
                x1 = max(float(b[2]) for b in boxes)
                if x0 >= 100 and x1 >= 180:
                    over = max(over, x1 - 250.0)
        text = doc[0].get_text() or ""
        doc.close()
        if "ё" in text or "Ё" in text:
            return False, "face-has-yo"
        lines = [ln.strip() for ln in text.splitlines()]
        for i, line in enumerate(lines):
            if not re.fullmatch(r"[AB][0-9A-Z]{26}", line):
                continue
            suf_line = ""
            for _j in range(i + 1, min(i + 5, len(lines))):
                if re.fullmatch(r"\d{5}", lines[_j]):
                    suf_line = lines[_j]
                    break
            if suf_line:
                flat = line + suf_line
                if (
                    len(flat) >= 32
                    and flat[17:19] == "G1"
                    and flat[19:22] == "018"
                    and flat[26:32] == "791103"
                    and (line[14], line[15]) not in {("0", "H"), ("1", "S")}
                ):
                    return False, f"slot018-bind:{line[14]}/{line[15]}"
            break
        g = int(_glyf_table_length(ff1)) if ff1 else 0
        # Do not hard-reject non-twin FF2 locally: final truth is Proton/OnlyPDF.
        # Some legal SBP faces mutate glyf in-band while still passing external checks.
        if g and _load_corpus_f1_twin_for_glyf(g) is not None:
            _ = _tbank_ff2_is_corpus_twin(ff1, height=h)
        if over > 0.25:
            return False, f"right-overshoot:+{over:.3f}"
        # Reject SBP IDs with non-digit timestamp (OnlyPDF/Proton STRUCTURE HARD).
        m_sid = re.search(r"\b([AB][0-9A-Z]{26})\b", text.replace("\n", ""))
        if m_sid:
            sid = m_sid.group(1)
            if not sid[1:11].isdigit():
                return False, f"sbp-ts-non-digit:{sid[1:11]}"
            if not sid[11:15].isdigit():
                return False, f"sbp-ref-non-digit:{sid[11:15]}"
        # Advisory only: do not block Telegram send on local tuple heuristic.
        # Source of truth is live Proton verdict in strict streak loop.
        flat = re.sub(r"\s+", "", text)
        m_full = re.search(
            r"([AB][0-9A-Z]{26})(70901|91103|30902|70402|60501|90502|21301)",
            flat,
        )
        if m_full:
            op = m_full.group(1) + m_full.group(2)
            if len(op) >= 32:
                tup = "|".join([
                    op[14], op[15], op[16], op[17:19], op[19:22], op[22:27], op[26:32],
                ])
                if tup in (
                    "1|6|0|G1|004|00117|770901",
                    "0|5|0|G1|014|00117|791103",
                    "1|D|0|G1|017|00117|791103",
                ):
                    logging.warning("SBP local burned tuple heuristic: %s (send to Proton)", tup)
                if tup.endswith("|00118|821301"):
                    parts = tup.split("|")
                    if len(parts) >= 7:
                        mk, ctrl, cls, slot = parts[0], parts[1], parts[3], parts[4]
                        if cls != "B1" or slot != "014" or (mk, ctrl) != ("0", "Y"):
                            return False, f"suffix-owner:{cls}/{slot}/{mk}{ctrl}"
    except Exception as exc:
        return False, f"ship-gate:{exc}"
    try:
        from tbank_emit import emit_invariants
        why = emit_invariants(pdf, channel="sbp")
        if why:
            if _live_gate_soft() and why.startswith("glyph mismatch"):
                print(f"  local-advisory {why} — send to bots", flush=True)
            else:
                return False, why
    except Exception as exc:
        return False, f"emit-inv:{exc}"
    try:
        from orig_match_gate import validate_vs_original
        ok_sz, why_sz = validate_vs_original("tbank_sbp", pdf, size_slack=1800)
        if not ok_sz:
            if _live_gate_soft():
                print(f"  local-advisory {why_sz} — send to bots", flush=True)
            else:
                return False, why_sz
    except Exception as exc:
        return False, f"orig-match:{exc}"
    return True, "ok"


async def main() -> int:
    from onlypdf_batch_cli import parse_batch_args
    from onlypdf_safe_names import CYR_NO_YO_TVERD

    args = parse_batch_args(
        default_out=_ROOT / "_test30_tbank_sbp",
        description="T-Bank SBP — OnlyPDF PASS×2 batch (canonical)",
    )
    n = args.n
    out = args.out

    rc = await generate_onlypdf_batch(
        out_dir=out,
        n=n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=_f1_ok,
        prefix="tbank_sbp",
        max_attempts=16,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
    )
    if rc == 0:
        joined = "".join(
            fitz.open(p)[0].get_text() for p in sorted(out.glob("tbank_sbp_*.pdf"))
        )
        miss, dig_miss = coverage_report(joined)
        print(
            f"coverage cyr_missing={''.join(miss) or 'нет'} "
            f"(alphabet без ё/ъ: {CYR_NO_YO_TVERD}) "
            f"dig_missing={''.join(dig_miss) or 'нет'}",
            flush=True,
        )
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
