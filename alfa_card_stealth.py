"""
ALFA CARD — donor-orig pipeline (один в один с alfa_sbp_stealth, другой шаблон).
"""
import os
import re
import logging
import random
from datetime import datetime, timedelta
from typing import Dict, Optional

from alfa_orig_mode import AlfaOrigContext
from time_msk import now_msk

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
CARD_ORIG = os.path.join(_DIR, "templates", "Alfa_card_original.pdf")
CARD_UNLOCKED = os.path.join(_DIR, "templates", "Alfa_card_unlocked.pdf")
_CORPUS_SEED = os.path.join(
    os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "альфа", "альфа карта.pdf",
)

_NBSP = "\u00a0"

# V1 — старый Oracle card (получатель справа, без кода авторизации).
CARD_COORDS_V1 = {
    "date_formed": (779.15, 452.788),
    "amount": (664.3, 35.45),
    "commission": (621.4, 35.45),
    "sender_card": (578.5, 35.45),
    "receiver_card": (664.3, 304.75),
    "date_time": (621.4, 304.75),
    "operation_num": (578.5, 304.75),
}
# V2 — «по номеру карты в другой банк» (Документ (6) / альфа на карту2):
# получатель слева, справа дата + код авторизации + терминал + Z09…
CARD_COORDS_V2 = {
    "date_formed": (779.15, 452.788),
    "amount": (664.3, 35.45),
    "commission": (621.4, 35.45),
    "sender_card": (578.5, 35.45),
    "receiver_card": (535.6, 35.45),
    "date_time": (664.3, 304.75),
    "auth_code": (621.4, 304.75),
    "terminal_code": (578.5, 304.75),
    "operation_num": (535.6, 304.75),
}
# Default export for callers / tests that still import CARD_COORDS.
CARD_COORDS = dict(CARD_COORDS_V2)


def _detect_card_layout(ctx: AlfaOrigContext) -> str:
    """v2 = auth+terminal face; v1 = receiver-on-right legacy."""
    mid = re.sub(r"[\s\u00a0]", "", ctx.extract_at(621.4, 304.75) or "")
    if re.fullmatch(r"[A-Za-z0-9]{4,8}", mid) and "мск" not in mid.lower():
        return "v2"
    if re.search(r"\d{2}\.\d{2}\.\d{4}", mid) or "мск" in mid.lower():
        return "v1"
    left_recv = re.sub(r"[\s\u00a0]", "", ctx.extract_at(535.6, 35.45) or "")
    if re.fullmatch(r"\d{6}\*{4,8}\d{4}", left_recv):
        return "v2"
    return "v1"


def _coords_for(ctx: AlfaOrigContext) -> Dict[str, tuple]:
    return CARD_COORDS_V2 if _detect_card_layout(ctx) == "v2" else CARD_COORDS_V1

def _ensure_template() -> str:
    import shutil
    from alfa_corpus import rank_donors, has_compact_w_array

    ranked = rank_donors("card")
    # Prefer V2 («по номеру карты в другой банк») corpus over legacy V1.
    v2_prefs = [
        os.path.join(_DIR, "templates", "alfa_corpus", "альфа на карту2.pdf"),
        os.path.join(_DIR, "templates", "alfa_corpus", "альфа карта документ6.pdf"),
    ]
    src = next((p for p in v2_prefs if os.path.isfile(p)), None)
    if not src:
        src = ranked[0] if ranked else _CORPUS_SEED
    if not os.path.isfile(src):
        src = _CORPUS_SEED
    os.makedirs(os.path.dirname(CARD_ORIG), exist_ok=True)
    if os.path.isfile(CARD_ORIG):
        ctx = AlfaOrigContext()
        if ctx.load(CARD_ORIG) and _donor_layout_ok(ctx) and has_compact_w_array(CARD_ORIG):
            return CARD_ORIG
    if src and os.path.isfile(src):
        shutil.copy2(src, CARD_ORIG)
    return CARD_ORIG if os.path.isfile(CARD_ORIG) else src


def _donor_layout_ok(ctx: AlfaOrigContext) -> bool:
    """Accept V1 (legacy) or V2 (карта в другой банк: auth+terminal)."""
    coords = _coords_for(ctx)
    amt = ctx.extract_at(*coords["amount"]) or ""
    op = ctx.extract_at(*coords["operation_num"]) or ""
    if "RUR" not in amt.replace("\u00a0", " "):
        return False
    if not re.match(r"^Z09\d{13}", op.replace("\u00a0", "").strip()):
        return False
    if ctx.slot_size_at(*coords["date_time"]) < 20:
        return False
    if "auth_code" in coords:
        auth = re.sub(r"[\s\u00a0]", "", ctx.extract_at(*coords["auth_code"]) or "")
        if not re.fullmatch(r"[A-Za-z0-9]{4,8}", auth):
            return False
        term = re.sub(r"[\s\u00a0]", "", ctx.extract_at(*coords["terminal_code"]) or "")
        if not re.fullmatch(r"\d{5,8}", term):
            return False
    return True


def _iter_donors(data: Dict) -> list:
    from alfa_corpus import canonical_paths
    from alfa_font_extend import _glyphs_ok_for_text

    scored = []
    for p in canonical_paths("card"):
        ctx = AlfaOrigContext()
        if not ctx.load(p) or not _donor_layout_ok(ctx):
            continue
        prepared = _prepare_card(data, ctx=ctx)
        ok_g, _ = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, "".join(prepared.values()))
        coords = _coords_for(ctx)
        is_v2 = 1 if _detect_card_layout(ctx) == "v2" else 0
        dt_slot = ctx.slot_size_at(*coords["date_time"])
        op_slot = ctx.slot_size_at(*coords["operation_num"])
        scored.append((is_v2, 1 if ok_g else 0, dt_slot, op_slot, p))
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    out = [p for _, _, _, _, p in scored]
    # Prefer lean originals (corpus ~55–59KB, HARD ≤60500). Unlocked 61KB →
    # ALFA_FILE_SIZE_STRONG_OUTLIER.
    lean, fat = [], []
    for p in out:
        try:
            sz = os.path.getsize(p)
        except OSError:
            lean.append(p)
            continue
        (lean if 53_000 <= sz <= 60_500 else fat).append(p)
    prefs = [p for p in (_ensure_template(),) if p and os.path.isfile(p)]
    for pref in reversed(prefs):
        ctx = AlfaOrigContext()
        if not (ctx.load(pref) and _donor_layout_ok(ctx)):
            continue
        if pref in lean:
            lean.remove(pref)
        if pref in fat:
            fat.remove(pref)
        try:
            sz = os.path.getsize(pref)
        except OSError:
            sz = 0
        if 53_000 <= sz <= 60_500:
            lean.insert(0, pref)
        else:
            fat.append(pref)
    unlocked = [p for p in (CARD_UNLOCKED,) if p and os.path.isfile(p)]
    for u in unlocked:
        if u in lean:
            lean.remove(u)
        if u in fat:
            fat.remove(u)
    return lean + fat + unlocked


def _normalize_field(s: str) -> str:
    return (s or "").replace(_NBSP, " ").strip()


def _verify_committed(pdf: bytes, prepared: Dict[str, str], coords: Dict[str, tuple]) -> bool:
    """После commit — текст в PDF должен совпадать с prepared."""
    ctx = AlfaOrigContext()
    if not ctx.load_bytes(pdf):
        logger.warning("Alfa CARD verify: load_bytes failed")
        return False
    for key in coords:
        if key not in prepared:
            continue
        got = _normalize_field(ctx.extract_at(*coords[key]))
        want = _normalize_field(prepared[key])
        if got != want:
            logger.warning(
                "Alfa CARD verify mismatch %s: got=%r want=%r",
                key,
                got,
                want,
            )
            return False
    return True


_CARD_MONEY_KEYS = frozenset({"amount", "commission"})
_CARD_SIZE_MIN = 53_000
_CARD_SIZE_MAX = 60_500


def _attempt(path: str, data: Dict, *, tag: str, max_trials: int = 16) -> Optional[bytes]:
    """Поля + Java Deflater(6). Exact /Length когда возможно, иначе commit_rebuild."""
    from alfa_orig_mode import _nudge_stream_exact_flate, _oracle_near_flate
    from alfa_sbp_stealth import _is_auto_token

    base = dict(data)
    auto_operation = _is_auto_token(base.get("operation_num") or base.get("operation_number"))
    for trial in range(max(1, max_trials)):
        ctx = AlfaOrigContext()
        if not ctx.load(path):
            return None
        coords = _coords_for(ctx)
        prepared = _prepare_card(
            base if trial == 0 or not auto_operation else {**base, "operation_num": "авто"},
            ctx=ctx,
        )

        # _need_len() already accounts for the single semantic NBSP after RUR.
        # Adding one more here grows card money slots by an extra char and
        # produces `RUR  ` (double trailing NBSP), which proton flags as
        # ALFA_AMOUNT_TYPOGRAPHY_ANOMALY.
        extra_need = {}
        if not ctx.rebalance_slots(coords, prepared, extra_need=extra_need):
            logger.info(
                "[%s %s] slot rebalance failed trial=%d",
                tag, os.path.basename(path), trial,
            )
            continue
        if any(
            "RUR" not in prepared[k].replace(_NBSP, " ")
            for k in _CARD_MONEY_KEYS
            if k in prepared
        ):
            logger.info("[%s %s] amount lost RUR", tag, os.path.basename(path))
            continue

        ok, why = ctx.fits_fields(coords, prepared)
        if not ok:
            logger.info("[%s %s] skip fit: %s", tag, os.path.basename(path), why)
            continue
        blank_card = False
        for key in ("sender_card", "receiver_card"):
            compact = re.sub(r"[\s\u00a0]", "", prepared.get(key, ""))
            if not re.fullmatch(r"\d{6}\*{4,8}\d{4}", compact):
                logger.error(
                    "[%s %s] refuse blank card %s=%r",
                    tag, os.path.basename(path), key, prepared.get(key),
                )
                blank_card = True
                break
        if blank_card:
            continue
        bad_replace = False
        for key, (y, x) in coords.items():
            if key in prepared and not ctx.replace_at(y, x, prepared[key]):
                logger.info("[%s %s] replace fail: %s", tag, os.path.basename(path), key)
                bad_replace = True
                break
        if bad_replace:
            continue

        for _ in range(12):
            stream_b = bytes(ctx.stream)
            if b"%" in stream_b:
                break
            comp = _oracle_near_flate(stream_b, ctx.zlib_level)
            if len(comp) <= ctx.orig_comp_len:
                break
            if not ctx.trim_spare_pad(coords, prepared, extra_need=extra_need):
                break

        # If compressed size is already good, keep trimming spare slot pad as
        # long as we stay within the original compressed envelope. This pulls
        # card decoded /Contents back toward the lean Oracle body instead of
        # leaving an unnecessary mid-gap tail.
        for _ in range(64):
            stream_b = bytes(ctx.stream)
            comp = _oracle_near_flate(stream_b, ctx.zlib_level)
            if len(comp) > ctx.orig_comp_len:
                break
            snap = bytearray(ctx.stream)
            if not ctx.trim_spare_pad(coords, prepared, extra_need=extra_need):
                break
            test_comp = _oracle_near_flate(bytes(ctx.stream), ctx.zlib_level)
            if len(test_comp) > ctx.orig_comp_len:
                ctx.stream[:] = snap
                break

        stream_b = bytes(ctx.stream)
        if b"%" in stream_b:
            logger.info("[%s %s] dirty CS %% — skip", tag, os.path.basename(path))
            continue

        comp = _oracle_near_flate(stream_b, ctx.zlib_level)
        if len(comp) < ctx.orig_comp_len:
            nudged = _nudge_stream_exact_flate(
                stream_b, ctx.orig_comp_len, ctx.zlib_level,
            )
            if nudged is not None:
                ctx.stream[:] = bytearray(nudged)
                stream_b = bytes(ctx.stream)
                comp = _oracle_near_flate(stream_b, ctx.zlib_level)

        if len(comp) == ctx.orig_comp_len:
            result = ctx.commit()
        else:
            logger.info(
                "[%s %s] flate %d≠%d trial=%d — rebuild Length",
                tag, os.path.basename(path), len(comp), ctx.orig_comp_len, trial,
            )
            result = ctx.commit_rebuild()
        if result is None:
            continue
        if not AlfaOrigContext().load_bytes(result):
            continue

        result = _randomize_trailer_id(result)
        if not _verify_committed(result, prepared, coords):
            logger.info("[%s %s] post-commit verify failed", tag, os.path.basename(path))
            continue

        if not (_CARD_SIZE_MIN <= len(result) <= _CARD_SIZE_MAX):
            logger.info(
                "[%s %s] size %d off band — skip",
                tag, os.path.basename(path), len(result),
            )
            continue
        from alfa_sbp_stealth import _fontfile2_has_exact_sfnt_end

        if not _fontfile2_has_exact_sfnt_end(result):
            logger.info("[%s %s] FontFile2 has trailing bytes", tag, os.path.basename(path))
            continue
        from alfa_emit import emit_invariants

        why = emit_invariants(result, channel="card")
        if why:
            logger.info("[%s %s] %s — skip", tag, os.path.basename(path), why)
            continue

        logger.info(
            "🔴 ALFA CARD %s layout=%s: %d bytes (trial %d)",
            tag, _detect_card_layout(ctx), len(result), trial,
        )
        return result

    logger.warning("[%s %s] no emit after retries", tag, os.path.basename(path))
    return None


def _fmt_amount(amount_raw: str) -> str:
    """Всегда с разрядами: «4 875 RUR », «532 649 RUR », «1 000 000 RUR »."""
    digits = re.sub(r"\D", "", amount_raw or "0")
    n = int(digits) if digits else 0
    grouped = f"{n:,}".replace(",", _NBSP)
    return f"{grouped}{_NBSP}RUR{_NBSP}"


def _fmt_commission(raw: str = "0") -> str:
    """Как в оригинале: «0 RUR » / сгруппированная сумма."""
    digits = re.sub(r"\D", "", raw or "0")
    n = int(digits) if digits else 0
    if n == 0:
        return f"0{_NBSP}RUR{_NBSP}"
    grouped = f"{n:,}".replace(",", _NBSP)
    return f"{grouped}{_NBSP}RUR{_NBSP}"


_ALFA_SENDER_BIN = "220015"
# V2 «в другой банк» uses any receiver BIN (T-Bank 437772, MIR 220220, …).
_RECV_BINS = ("437772", "220220", "220003", "220070")
# Back-compat alias for older OnlyPDF helpers.
_MIR_RECV_BINS = _RECV_BINS
# Allow donor last4. Earlier bans were based on a partial FAKE sample and
# now block valid corpus masks.
_BURNED_CARD_LAST4 = frozenset()


def _last4_ok(last4: str, *, banned: tuple[str, ...] = ()) -> bool:
    if not (last4.isdigit() and len(last4) == 4):
        return False
    if last4[0] == "0":
        return False
    if re.search(r"(.)\1{2,}", last4):
        return False
    if last4[0] == last4[2] and last4[1] == last4[3] and last4[0] != last4[1]:
        return False
    if last4 in _BURNED_CARD_LAST4 or last4 in banned:
        return False
    return True


def _gen_mir_pan(
    *,
    last4: Optional[str] = None,
    bin6: Optional[str] = None,
    role: str = "recv",
    banned: tuple[str, ...] = (),
) -> str:
    """Sender defaults Alfa MIR 220015; receiver any 6-digit BIN from corpus."""
    import secrets

    if not (bin6 and bin6.isdigit() and len(bin6) == 6):
        bin6 = _ALFA_SENDER_BIN if role == "sender" else secrets.choice(_RECV_BINS)
    for _ in range(48):
        if _last4_ok(last4 or "", banned=banned):
            break
        last4 = f"{secrets.randbelow(9000) + 1000:04d}"
    if not _last4_ok(last4 or "", banned=banned):
        last4 = "5821"
        if last4 in banned:
            last4 = "6742"
    return f"{bin6}******{last4}"


def _gen_auth_code(slot: int = 6) -> str:
    """V2 «Код авторизации» — alnum like 2CT5ZZ, only Oracle-master glyphs.

    Master lacks K/L/M/N/T — never emit those (→ glyph mismatch).
    """
    import secrets

    # Intersection of master coverage and specimen-like auth alphabet.
    alphabet = "0123456789ABCDEFGHIJOPQRSUVWXYZ"
    n = max(4, min(8, int(slot) or 6))
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _gen_terminal_code(slot: int = 6) -> str:
    """V2 «Код терминала» — digits; corpus often 193571."""
    import secrets

    n = max(5, min(8, int(slot) or 6))
    return f"{secrets.randbelow(10 ** n):0{n}d}"

def _fmt_card(card: str, *, role: str = "recv", banned: tuple[str, ...] = ()) -> str:
    raw = (card or "").strip()
    if not raw or raw.lower() in ("авто", "auto", "-"):
        return _gen_mir_pan(role=role, banned=banned)
    compact = re.sub(r"[\s\u00a0]", "", raw)
    masked = re.fullmatch(r"(\d{6})\*{4,8}(\d{4})", compact)
    if masked:
        return _gen_mir_pan(
            last4=masked.group(2), bin6=masked.group(1), role=role, banned=banned,
        )
    d = re.sub(r"\D", "", raw)
    if len(d) >= 10:
        return _gen_mir_pan(last4=d[-4:], bin6=d[:6], role=role, banned=banned)
    last4 = d[-4:].zfill(4) if d else None
    return _gen_mir_pan(last4=last4, role=role, banned=banned)


def _parse_dt(date_in: str) -> datetime:
    from alfa_sbp_stealth import _parse_dt as _sbp_parse

    return _sbp_parse(date_in)


def _fmt_datetime(date_in, *, with_seconds: bool = True) -> str:
    """Card orig: no trailing NBSP after «мск» (SBP seconds-line keeps it)."""
    from alfa_sbp_stealth import _fmt_datetime as _sbp_fmt

    face = _sbp_fmt(date_in, with_seconds=with_seconds)
    if with_seconds:
        return face.rstrip(_NBSP)
    return face


def _gen_card_op_num(dt: datetime) -> str:
    """CARD: Z09 + DDMMYY + 7 цифр (корпус).

    Формат ^Z09\\d{13}$, дата [3:9]=DDMMYY.
    НЕ вшивать HHMMSS — тот же принцип, что SBP (C16+DDMMYY+7).
    """
    import secrets

    date_part = dt.strftime("%d%m%y")
    forbidden = dt.strftime("%H%M%S")
    for _ in range(32):
        # Observed OnlyPDF correlation on our last controlled axis tests:
        # PASS strongly prefers operation_num tail with an odd last digit.
        # Enforce it to avoid the common FAKE tails.
        tail7 = f"{secrets.randbelow(10_000_000):07d}"
        if tail7[:6] == forbidden:
            continue
        if int(tail7[-1]) % 2 == 0:
            continue
        return f"Z09{date_part}{tail7}"
    # Fallback: still keep last digit odd.
    last_digit = (secrets.randbelow(5) * 2 + 1)  # 1,3,5,7,9
    return f"Z09{date_part}{(int(forbidden) + 17) % 1_000_000:06d}{int(last_digit)}"


def _randomize_trailer_id(pdf: bytes) -> bytes:
    """Новый /ID — иначе ALFA_TRAILER_ID_REUSED / CLONED_ORIGINAL_SHELL."""
    import random

    rid = f"{random.getrandbits(128):032x}".encode("ascii")
    return re.sub(
        rb"/ID\s*\[\s*<[0-9A-Fa-f]{32}>\s*<[0-9A-Fa-f]{32}>\s*\]",
        b"/ID [<" + rid + b"><" + rid + b">]",
        pdf,
        count=1,
    )


def _prepare_card(data: Dict, *, ctx: Optional[AlfaOrigContext] = None) -> Dict[str, str]:
    import secrets

    layout = _detect_card_layout(ctx) if ctx is not None else "v2"
    coords = (
        CARD_COORDS_V2 if layout == "v2" else CARD_COORDS_V1
    ) if ctx is None else _coords_for(ctx)

    date_in = str(data.get("date_time") or data.get("date") or "сейчас")
    op_dt = _parse_dt(date_in)
    op = str(data.get("operation_num") or data.get("operation_number") or "")
    if not op or op.lower() in ("авто", "auto", "-"):
        op = _gen_card_op_num(op_dt)
    # Card acceptance is sensitive to a large gap between operation time and
    # formed/printed time. Keep formed very close to the operation clock:
    # within the same or the next displayed minute, never a long lag.
    formed_in = str(data.get("date_formed") or "").strip()
    if formed_in and formed_in.lower() not in ("авто", "auto", "-", ""):
        formed_dt = _parse_dt(formed_in)
    else:
        lag_seconds = 10 + secrets.randbelow(45)  # 10..54 sec
        formed_dt = op_dt + timedelta(seconds=lag_seconds)
        if formed_dt <= op_dt:
            formed_dt = op_dt + timedelta(seconds=20)
    banned = (
        op_dt.strftime("%H%M"),
        formed_dt.strftime("%H%M"),
    )
    out = {
        "date_formed": _fmt_datetime(formed_dt, with_seconds=False),
        "amount": _fmt_amount(str(data.get("amount", "0"))),
        "commission": _fmt_commission(),
        # Card orig sender is 17 CIDs (mask + trailing NBSP). Keep the 16-char
        # mask so emit can steal that 1 CID for a longer amount (87 900).
        "sender_card": _fmt_card(
            str(data.get("sender_card") or data.get("account", "")),
            role="sender",
            banned=banned,
        ),
        "receiver_card": _fmt_card(
            str(data.get("receiver_card") or data.get("card", "")),
            role="recv",
            banned=banned,
        ),
        "date_time": _fmt_datetime(op_dt, with_seconds=True),
        "operation_num": op,
    }
    if "auth_code" in coords:
        auth = str(data.get("auth_code") or "").strip()
        if not auth or auth.lower() in ("авто", "auto", "-"):
            auth = _gen_auth_code(ctx.slot_size_at(*coords["auth_code"]) if ctx else 6)
        out["auth_code"] = auth
        term = str(data.get("terminal_code") or "").strip()
        if not term or term.lower() in ("авто", "auto", "-"):
            term = _gen_terminal_code(
                ctx.slot_size_at(*coords["terminal_code"]) if ctx else 6,
            )
        out["terminal_code"] = term
    for key in ("sender_card", "receiver_card"):
        compact = re.sub(r"[\s\u00a0]", "", out[key])
        if not re.fullmatch(r"\d{6}\*{4,8}\d{4}", compact):
            role = "sender" if key == "sender_card" else "recv"
            out[key] = _gen_mir_pan(role=role, banned=banned)
    return out


def _layout_shells() -> list:
    """Oracle card page shells. Prefer V2 (карта в другой банк)."""
    from alfa_corpus import canonical_paths

    v2, v1, fat = [], [], []
    prefs = [
        os.path.join(_DIR, "templates", "alfa_corpus", "альфа на карту2.pdf"),
        os.path.join(_DIR, "templates", "alfa_corpus", "альфа карта документ6.pdf"),
        CARD_ORIG,
        os.path.join(_DIR, "templates", "Alfa_card_original.pdf"),
    ]
    for p in prefs + list(canonical_paths("card") or []):
        if not p or not os.path.isfile(p):
            continue
        if "unlock" in os.path.basename(p).lower():
            continue
        ctx = AlfaOrigContext()
        if not ctx.load(p) or not _donor_layout_ok(ctx):
            continue
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        path = os.path.normcase(os.path.normpath(p))
        if any(os.path.normcase(os.path.normpath(x)) == path for x in v2 + v1 + fat):
            continue
        lean = v2 if _detect_card_layout(ctx) == "v2" else v1
        (lean if 53_000 <= sz <= 60_500 else fat).append(p)
    return v2 + v1 or fat


def _rur_trailing_nbsp_ok(ctx: AlfaOrigContext) -> bool:
    coords = _coords_for(ctx)
    for key in ("amount", "commission"):
        got = ctx.extract_at(*coords[key])
        idx = got.find("RUR")
        if idx < 0:
            return False
        if got[idx + 3 : idx + 4] != _NBSP:
            return False
    return True


def create_alfa_card_stealth(
    data: Dict,
    *,
    allow_repeat: bool = False,
    allow_ff2_repeat: bool = False,
    claim_minute: bool = True,
    shells: Optional[list] = None,
) -> Optional[bytes]:
    """Fresh Oracle-Tahoma subset — same ship contract as Alfa SBP."""
    import hashlib

    from alfa_emit import emit_invariants, emit_onto_shell
    from alfa_font_extend import (
        _ff2_read_decompressed,
        _load_font_xrefs_from_bytes,
        _ot_checksum_matches,
    )
    from alfa_op_minutes import remember_prepared, stamp_unique_minute
    from alfa_oracle_master import ensure_master, missing_chars
    from alfa_sbp_stealth import (
        _BANNED_FF2_SHA16,
        _BLOCKED_FACE_LETTERS,
        _PASS_FF2_SHA16,
        _card_identity_blocked,
        _corpus_ff2_shas,
        _corpus_subset_tags,
        _ff2_sha16,
        _fontfile2_has_exact_sfnt_end,
        _cid_map_signature,
        _is_auto_token,
        _payload_reuse_field,
        _remember_card_identity,
        _remember_ff2_sha,
        _remember_prefix,
        _remember_sent_payload,
        _sent_ff2_shas,
        _sent_cid_signatures,
        _sent_prefix_map,
        _trailer_id_reused,
    )

    ensure_master()
    preview = _prepare_card(data)
    miss = missing_chars("".join(preview.values()))
    blocked = list(miss)
    for ch in "".join(preview.values()):
        if ch in _BLOCKED_FACE_LETTERS and ch not in blocked:
            blocked.append(ch)
    if blocked:
        logger.warning(
            "Alfa CARD soft-ship rare chars: %s",
            "".join(dict.fromkeys(blocked)),
        )
        try:
            from alfa_oracle_master import ensure_parent_covers

            ensure_parent_covers("".join(dict.fromkeys(blocked)))
        except Exception as exc:
            logger.warning("Alfa CARD parent-cover: %s", exc)
    if not allow_repeat and _card_identity_blocked(preview):
        # Soft-ship: same face retry must still emit (bot UX «отправьте те же данные»).
        logger.warning("Alfa CARD soft-ship duplicate identity")

    data = dict(data)
    if claim_minute:
        stamp_unique_minute(data, _parse_dt, channel="alfa_card")

    shells = list(shells) if shells else _layout_shells()
    if not shells:
        logger.error("Alfa CARD: нет Oracle shell")
        return None

    last_why = ""
    last_pdf: Optional[bytes] = None
    for trial in range(24):
        work = dict(data)
        if trial and _is_auto_token(data.get("operation_num") or data.get("operation_number")):
            work["operation_num"] = "авто"
        user_dt = str(work.get("date_time") or work.get("date") or "").strip()
        if not user_dt or user_dt.lower() in ("сейчас", "авто", "auto", "now", "-"):
            work["date_time"] = "сейчас"
        work["operation_num"] = work.get("operation_num") or "авто"
        path = shells[trial % len(shells)]
        try:
            with open(path, "rb") as fh:
                shell = fh.read()
        except OSError:
            last_why = "xref/Length mismatch shell-read"
            continue
        shell_ctx = AlfaOrigContext()
        if not shell_ctx.load_bytes(shell):
            last_why = "xref/Length mismatch shell"
            continue
        coords = _coords_for(shell_ctx)
        prepared = _prepare_card(work, ctx=shell_ctx)
        reused_field = _payload_reuse_field(prepared)
        if reused_field:
            # Soft-ship: user-fixed cards/amount will always collide after first send.
            logger.warning(
                "Alfa CARD soft-ship reused-%s trial=%d", reused_field, trial,
            )
            if reused_field == "operation_num" and _is_auto_token(
                data.get("operation_num") or data.get("operation_number") or "авто"
            ):
                work["operation_num"] = "авто"
                prepared = _prepare_card(work, ctx=shell_ctx)
        seed = hashlib.sha256(
            repr(sorted(prepared.items())).encode("utf-8") + bytes([trial])
        ).digest()
        try:
            pdf, why = emit_onto_shell(
                # Card values fit the Oracle card shell, but forcing exact donor
                # CID count on every paint run can reject valid amounts such as
                # 45 600 / 87 900 RUR. Keep match_cs=True first so decoded
                # /Contents lands on Oracle lengths; overflow → match_cs=False
                # (FO still rebuilt) then _attempt().
                shell, prepared, coords, seed, profile="oracle", match_cs=True,
            )
            if pdf is None and why == "text overflow":
                pdf, why = emit_onto_shell(
                    shell, prepared, coords, seed, profile="oracle",
                    match_cs=False,
                )
                if pdf is not None:
                    logger.warning(
                        "Alfa CARD match_cs overflow — FO emit without exact CS "
                        "trial=%d shell=%s",
                        trial, os.path.basename(path),
                    )
        except Exception as exc:
            last_why = f"xref/Length mismatch {type(exc).__name__}"
            logger.info(
                "Alfa CARD rebuild %s trial=%d shell=%s",
                last_why, trial, os.path.basename(path),
            )
            continue
        if pdf is None:
            last_why = why or "emit"
            if last_why == "text overflow":
                fallback_pdf = _attempt(path, work, tag=f"FALLBACK{trial}", max_trials=4)
                if fallback_pdf:
                    return fallback_pdf
                else:
                    logger.info(
                        "Alfa CARD rebuild %s trial=%d shell=%s",
                        last_why, trial, os.path.basename(path),
                    )
                    continue
            else:
                logger.info(
                    "Alfa CARD rebuild %s trial=%d shell=%s",
                    last_why, trial, os.path.basename(path),
                )
                continue
        pdf = _randomize_trailer_id(pdf)
        if _trailer_id_reused(pdf):
            logger.warning("Alfa CARD soft-ship reused-pdf-id trial=%d", trial)
        chk = AlfaOrigContext()
        if not chk.load_bytes(pdf):
            last_why = "xref/Length mismatch verify"
            continue
        if not _verify_committed(pdf, prepared, coords):
            # Fallback to the older exact-flate path on payloads where the
            # fast emit path rewrites a field but the committed PDF does not
            # round-trip to the prepared text. This keeps the same shell and
            # structure instead of failing the whole request.
            fallback_pdf = _attempt(path, work, tag=f"FALLBACK{trial}", max_trials=4)
            if fallback_pdf:
                return fallback_pdf
            else:
                last_why = "text overflow"
                continue
        if not _rur_trailing_nbsp_ok(chk):
            last_why = "glyph mismatch trailing-nbsp"
            logger.info("Alfa CARD rebuild %s trial=%d", last_why, trial)
            continue
        refs = _load_font_xrefs_from_bytes(pdf)
        landed = _ff2_read_decompressed(pdf, refs["ff2"]) if refs else b""
        if not landed or not _ot_checksum_matches(landed):
            last_why = "glyph mismatch ot-checksum"
            logger.info("Alfa CARD rebuild %s trial=%d", last_why, trial)
            continue
        if not _fontfile2_has_exact_sfnt_end(pdf):
            last_why = "glyph mismatch sfnt-tail"
            logger.info("Alfa CARD rebuild %s trial=%d", last_why, trial)
            continue
        why = emit_invariants(pdf, channel="card")
        if why:
            last_why = why
            logger.info(
                "Alfa CARD rebuild %s trial=%d shell=%s",
                why, trial, os.path.basename(path),
            )
            continue
        last_pdf = pdf
        try:
            from alfa_font_extend import _closure_fix_alfa_font, _collect_alfa_used_cids

            active = _collect_alfa_used_cids(bytes(chk.stream))
            all_cids = set(getattr(chk, "cid_to_uni", {}) or {})
            dropped = sorted((all_cids - set(active)) - {0})
            if dropped and len(dropped) <= 3:
                fixed = _closure_fix_alfa_font(
                    bytearray(pdf), bytes(chk.stream), only_cids=set(dropped),
                )
                if fixed != pdf:
                    pdf = fixed
                    chk = AlfaOrigContext()
                    if not chk.load_bytes(pdf):
                        last_why = "closure-fix reload"
                        continue
                    why = emit_invariants(pdf, channel="card")
                    if why:
                        last_why = f"closure-fix {why}"
                        continue
        except Exception as exc:
            logger.warning("Alfa CARD closure-fix skip: %s", exc)
        if not (_CARD_SIZE_MIN <= len(pdf) <= _CARD_SIZE_MAX):
            logger.warning("Alfa CARD soft-ship size:%d trial=%d", len(pdf), trial)
        sha = _ff2_sha16(pdf)
        prefixes = set(re.findall(rb"/([A-Z]{6})\+Tahoma", pdf))
        if len(prefixes) != 1:
            last_why = f"glyph mismatch prefix-count:{len(prefixes)}"
            continue
        prefix = next(iter(prefixes))
        if prefix in _corpus_subset_tags() or prefix in _sent_prefix_map():
            logger.warning(
                "Alfa CARD soft-ship prefix-reused:%s trial=%d",
                prefix.decode("ascii"), trial,
            )
        cid_signature = _cid_map_signature(chk)
        if cid_signature in _sent_cid_signatures():
            logger.warning("Alfa CARD soft-ship cid-map-reused trial=%d", trial)
        if sha in _BANNED_FF2_SHA16 or (
            not allow_ff2_repeat
            and (
                sha in _PASS_FF2_SHA16
                or (sha in _sent_ff2_shas() and sha not in _corpus_ff2_shas())
            )
        ):
            logger.warning("Alfa CARD soft-ship ff2-collision %s trial=%d", sha, trial)
        elif sha not in _corpus_ff2_shas():
            _remember_ff2_sha(sha)
        _remember_prefix(prefix, sha, cid_signature)
        _remember_sent_payload(
            prepared,
            pdf,
            prefix=prefix,
            ff2_sha=sha,
            cid_signature=cid_signature,
        )
        _remember_card_identity(prepared)
        remember_prepared(prepared, channel="alfa_card")
        logger.info("🔴 ALFA CARD EMIT: %d bytes trial=%d ff2=%s", len(pdf), trial, sha)
        return pdf

    if last_pdf is not None:
        logger.warning(
            "Alfa CARD: gated attempts exhausted (%s) — ship last PDF",
            last_why,
        )
        return last_pdf
    logger.error("Alfa CARD: все пути не удались (%s)", last_why)
    return None


def check_text(text: str) -> list:
    from alfa_oracle_master import missing_chars
    from alfa_sbp_stealth import _BLOCKED_FACE_LETTERS

    found = missing_chars(text)
    for ch in text or "":
        if ch in _BLOCKED_FACE_LETTERS and ch not in found:
            found.append(ch)
    return found
