# -*- coding: utf-8 -*-
"""Validate generated PDF against canonical original template metrics."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent


def _orig_cs_len(path: Path) -> Optional[int]:
    try:
        from alfa_orig_mode import AlfaOrigContext

        ctx = AlfaOrigContext()
        if ctx.load(str(path)):
            return len(bytes(ctx.stream))
    except Exception:
        pass
    return None


def validate_vs_original(
    method: str,
    pdf: bytes,
    *,
    size_slack: Optional[int] = None,
    cs_slack: Optional[int] = 64,
) -> Tuple[bool, str]:
    """Check size band (and Oracle CS when applicable) vs profile original."""
    from tools.receipt_method_profiles import effective_size_band, get_profile

    try:
        profile = get_profile(method)
    except KeyError:
        return True, "ok"

    lo, hi = effective_size_band(profile)
    n = len(pdf)
    if n < lo or n > hi:
        ref = profile.references[0] if profile.references else None
        ref_sz = ref.stat().st_size if ref and ref.is_file() else 0
        return False, f"size-band:{n} not in [{lo},{hi}] orig={ref_sz}"

    ref_path = next((p for p in profile.references if p.is_file()), None)
    if ref_path is not None and size_slack is not None:
        ref_sz = ref_path.stat().st_size
        if abs(n - ref_sz) > size_slack:
            return False, f"size-drift:{ref_sz}→{n} ({n - ref_sz:+d})"

    if method.startswith("alfa_") and method != "alfa_phone" and ref_path and cs_slack is not None:
        ocs = _orig_cs_len(ref_path)
        if ocs is not None:
            from alfa_orig_mode import AlfaOrigContext

            ctx = AlfaOrigContext()
            if ctx.load_bytes(pdf):
                gcs = len(bytes(ctx.stream))
                if abs(gcs - ocs) > cs_slack:
                    return False, f"cs-drift:{ocs}→{gcs} ({gcs - ocs:+d})"

    if method.startswith("tbank_"):
        from tbank_emit import emit_invariants

        ch = method.split("_", 1)[1]
        why = emit_invariants(pdf, channel=ch)
        if why:
            return False, why
    elif method.startswith("alfa_"):
        from alfa_emit import emit_invariants

        ch = method.split("_", 1)[1]
        why = emit_invariants(pdf, channel=ch)
        if why:
            return False, why

    return True, "ok"


def wrap_validate(method: str, inner, **kwargs):
    """Run channel-local checks, then orig size/invariants."""

    def _fn(pdf: bytes) -> Tuple[bool, str]:
        ok, why = inner(pdf)
        if not ok:
            return False, why
        return validate_vs_original(method, pdf, **kwargs)

    return _fn


def tbank_sbp_validate(pdf: bytes) -> Tuple[bool, str]:
    """Combined T-Bank SBP ship gate: structure + orig band + emit_invariants."""
    from gen_onlypdf30 import _f1_ok

    ok, why = _f1_ok(pdf)
    if not ok:
        return False, why
        ok2, why2 = validate_vs_original("tbank_sbp", pdf, size_slack=1800)
    if not ok2:
        return False, why2
    return True, "ok"
