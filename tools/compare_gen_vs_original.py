# -*- coding: utf-8 -*-
"""Compare one generated PDF per live channel vs canonical original template."""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_DIR))

import fitz  # noqa: E402


def _ff2_decompressed_len(pdf: bytes) -> Optional[int]:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        for xref in range(1, doc.xref_length()):
            if b"FontFile2" not in (doc.xref_object(xref) or "").encode():
                continue
            raw = doc.xref_stream(xref)
            doc.close()
            return len(raw) if raw else None
        doc.close()
    except Exception:
        pass
    return None


def _cs_len(pdf: bytes) -> Optional[int]:
    try:
        from alfa_orig_mode import AlfaOrigContext

        ctx = AlfaOrigContext()
        if ctx.load_bytes(pdf):
            return len(bytes(ctx.stream))
    except Exception:
        pass
    try:
        import tbank_unlock_template as tut

        doc = fitz.open(stream=pdf, filetype="pdf")
        cs = tut._content_stream_bytes(doc)
        doc.close()
        return len(cs) if cs else None
    except Exception:
        return None


def _producer(pdf: bytes) -> str:
    m = re.search(rb"/Producer\s*\(([^)]+)\)", pdf[:120_000])
    return m.group(1).decode("latin-1", "replace")[:60] if m else ""


def _keywords(pdf: bytes) -> str:
    m = re.search(rb"/Keywords\s*\(([^)]+)\)", pdf[:120_000])
    return m.group(1).decode("latin-1", "replace")[:80] if m else ""


def metrics(pdf: bytes) -> Dict[str, object]:
    doc = fitz.open(stream=pdf, filetype="pdf")
    page = doc[0]
    out = {
        "size": len(pdf),
        "sha16": hashlib.sha256(pdf).hexdigest()[:16],
        "page": (round(float(page.rect.width), 1), round(float(page.rect.height), 1)),
        "producer": _producer(pdf),
        "keywords": _keywords(pdf),
        "ff2_dec": _ff2_decompressed_len(pdf),
        "cs_dec": _cs_len(pdf),
    }
    doc.close()
    return out


def diff_metrics(orig: Dict[str, object], gen: Dict[str, object]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key in ("size", "ff2_dec", "cs_dec"):
        o, g = orig.get(key), gen.get(key)
        if o is None or g is None:
            continue
        delta = int(g) - int(o)
        if delta:
            out[key] = f"{o} → {g} ({delta:+d})"
    if orig.get("producer") != gen.get("producer"):
        out["producer"] = f"{orig.get('producer')!r} → {gen.get('producer')!r}"
    if orig.get("page") != gen.get("page"):
        out["page"] = f"{orig.get('page')} → {gen.get('page')}"
    return out


CHANNELS: Tuple[Tuple[str, str, Callable, dict], ...] = (
    (
        "tbank_sbp",
        "templates/T_sbp_original.pdf",
        lambda d: __import__("tbank_sbp_stealth", fromlist=["create_tbank_sbp_stealth"]).create_tbank_sbp_stealth(d),
        {
            "date_time": "21.04.2026 20:54:15",
            "amount": "93934",
            "sender": "Павел Соколов",
            "receiver": "Роман Соколов",
            "phone": "+7 (315) 697-20-48",
            "recipient_bank": "Сбербанк",
            "sbp_id": "авто",
            "receipt_num": "авто",
        },
    ),
    (
        "tbank_phone",
        "templates/T_phone_original.pdf",
        lambda d: __import__("tbank_phone_stealth", fromlist=["create_tbank_phone_stealth"]).create_tbank_phone_stealth(d),
        {
            "date_time": "21.04.2026 20:54:15",
            "amount": "5000",
            "sender": "Иван Иванов",
            "receiver": "Петр Петров",
            "phone": "+7 (900) 123-45-67",
        },
    ),
    (
        "tbank_nocomm",
        "templates/T_nocomm_original.pdf",
        lambda d: __import__("tbank_nocomm_stealth", fromlist=["create_tbank_nocomm_stealth"]).create_tbank_nocomm_stealth(d),
        {
            "date_time": "21.04.2026 20:54:15",
            "amount": "5000",
            "sender": "Иван Иванов",
            "receiver": "Петр Петров",
            "phone": "+7 (900) 123-45-67",
        },
    ),
    (
        "alfa_sbp",
        "templates/Alfa_sbp_original.pdf",
        lambda d: __import__("alfa_sbp_stealth", fromlist=["create_alfa_sbp_stealth"]).create_alfa_sbp_stealth(d),
        {
            "date_time": "16.08.2026 14:10:27",
            "amount": "33470",
            "receiver": "Галина Ивановна Б",
            "phone": "+7 (916) 551-20-44",
            "recipient_bank": "Т-Банк",
            "sbp_id": "авто",
            "operation_num": "авто",
            "account": "авто",
        },
    ),
    (
        "alfa_card",
        "templates/Alfa_card_original.pdf",
        lambda d: __import__("alfa_card_stealth", fromlist=["create_alfa_card_stealth"]).create_alfa_card_stealth(d, allow_repeat=True),
        None,  # filled below from gen_onlypdf30 payload
    ),
    (
        "alfa_phone",
        "templates/Alfa_phone_original.pdf",
        lambda d: __import__("alfa_phone_stealth", fromlist=["create_alfa_phone_stealth"]).create_alfa_phone_stealth(d),
        {
            "date_time": "01.08.2026 10:00:00",
            "amount": "15000",
            "receiver": "Петр Петров",
            "phone": "+7 (900) 123-45-67",
        },
    ),
    (
        "sber_sbp",
        "templates/S_sbp_original.pdf",
        lambda d: __import__("sber_sbp_stealth", fromlist=["create_sber_sbp_stealth"]).create_sber_sbp_stealth(d),
        {
            "date_time": "01.08.2026 12:00:00",
            "amount": "15000",
            "sender": "Иван Иванович И.",
            "receiver": "Петр Петрович П",
            "phone": "+7 (900) 111-22-33",
            "recipient_bank": "Сбербанк",
            "sbp_id": "авто",
        },
    ),
    (
        "sber_phone",
        "templates/sber_shells/сбер по номеру телефона на сбер.pdf",
        lambda d: __import__("sber_phone_stealth", fromlist=["create_sber_phone_stealth"]).create_sber_phone_stealth(d),
        {
            "date_time": "01.08.2026 12:00:00",
            "amount": "15000",
            "sender": "Иван Иванович И.",
            "receiver": "Петр Петрович П",
            "phone": "+7 (900) 111-22-33",
        },
    ),
)


def main() -> int:
    bad = 0
    for item in CHANNELS:
        if len(item) == 4 and item[3] is None:
            import sys
            sys.path.insert(0, str(_DIR))
            from gen_onlypdf30_alfa_card import _payload as card_payload
            name, orig_rel, gen_fn, _ = item
            payload = card_payload(0, 0)
        else:
            name, orig_rel, gen_fn, payload = item
        orig_path = _ROOT / orig_rel
        print(f"\n=== {name} ===")
        if not orig_path.is_file():
            print(f"  SKIP missing orig: {orig_rel}")
            bad += 1
            continue
        orig_b = orig_path.read_bytes()
        om = metrics(orig_b)
        pdf = gen_fn(dict(payload))
        if not pdf:
            print("  GEN None")
            bad += 1
            continue
        gm = metrics(pdf)
        inv = ""
        try:
            if name.startswith("alfa"):
                from alfa_emit import emit_invariants

                inv = emit_invariants(pdf, channel=name.split("_", 1)[1])
            elif name.startswith("tbank"):
                from tbank_emit import emit_invariants

                inv = emit_invariants(pdf, channel=name.split("_", 1)[1])
        except Exception as exc:
            inv = f"err:{exc}"
        print(f"  orig size={om['size']} ff2={om.get('ff2_dec')} cs={om.get('cs_dec')}")
        print(f"  gen  size={gm['size']} ff2={gm.get('ff2_dec')} cs={gm.get('cs_dec')} inv={inv or 'ok'}")
        d = diff_metrics(om, gm)
        if d:
            for k, v in d.items():
                print(f"  Δ {k}: {v}")
        else:
            print("  Δ (size/ff2/cs/producer/page match)")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
