# -*- coding: utf-8 -*-
"""Probe: T-Bank SBP donor-orig only vs OnlyPDF."""
from __future__ import annotations

import asyncio
import logging
import random
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

logging.disable(logging.INFO)

from onlypdf_gate import open_onlypdf_client, onlypdf_verdict  # noqa: E402
from onlypdf_safe_names import BANKS_MULTI, pick_receiver_short, pick_sender_pair  # noqa: E402
from tbank_sbp_stealth import _prepare_sbp_data, _try_donor_orig_mode  # noqa: E402


async def main() -> int:
    client, cfg = await open_onlypdf_client()
    out = _ROOT / "output" / "tbank_donor_probe"
    out.mkdir(parents=True, exist_ok=True)
    res: list[str] = []
    try:
        for i in range(10):
            rng = random.Random(3000 + i)
            fn, ln = pick_sender_pair(i + 10, 1)
            data = {
                "date_time": f"23.07.2026, {8 + i:02d}:20:00",
                "amount": str(rng.randint(2500, 35000)),
                "sender": f"{fn} {ln}",
                "phone": f"+7 (91{i % 10}) 222-33-44",
                "receiver": pick_receiver_short(i, 1),
                "recipient_bank": BANKS_MULTI[i % len(BANKS_MULTI)],
                "operation_num": "авто",
                "receipt_num": "авто",
                "sbp_id": "авто",
            }
            prepared = _prepare_sbp_data(data)
            pdf = _try_donor_orig_mode(prepared)
            if not pdf:
                print(i, "GEN_FAIL", fn, ln, flush=True)
                res.append("GEN_FAIL")
                continue
            path = out / f"d{i:02d}.pdf"
            path.write_bytes(pdf)
            v = await onlypdf_verdict(client, path, cfg)
            print(i, v, len(pdf), fn, ln, flush=True)
            res.append(v)
    finally:
        await client.disconnect()
    print(
        "SUMMARY",
        res,
        "PASS",
        res.count("PASS"),
        "FAKE",
        res.count("FAKE"),
        "GEN_FAIL",
        res.count("GEN_FAIL"),
        flush=True,
    )
    return 0 if res.count("FAKE") == 0 and res.count("PASS") >= 8 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
