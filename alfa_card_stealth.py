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

# (y, x) — значения из корпуса card
CARD_COORDS = {
    "date_formed": (779.15, 452.788),
    "amount": (664.3, 35.45),
    "commission": (621.4, 35.45),
    "sender_card": (578.5, 35.45),
    "receiver_card": (664.3, 304.75),
    "date_time": (621.4, 304.75),
    "operation_num": (578.5, 304.75),
}

def _ensure_template() -> str:
    import shutil
    from alfa_corpus import rank_donors, has_compact_w_array

    ranked = rank_donors("card")
    src = ranked[0] if ranked else _CORPUS_SEED
    if not os.path.isfile(src):
        src = _CORPUS_SEED
    os.makedirs(os.path.dirname(CARD_ORIG), exist_ok=True)
    if os.path.isfile(CARD_ORIG):
        ctx = AlfaOrigContext()
        if (
            ctx.load(CARD_ORIG)
            and ctx.slot_size_at(621.4, 304.75) >= 23
            and has_compact_w_array(CARD_ORIG)
        ):
            return CARD_ORIG
    if src and os.path.isfile(src):
        shutil.copy2(src, CARD_ORIG)
    return CARD_ORIG if os.path.isfile(CARD_ORIG) else src


def _donor_layout_ok(ctx: AlfaOrigContext) -> bool:
    """Отсечь чужую вёрстку (напр. «альфа на карту2» — другие координаты)."""
    amt = ctx.extract_at(*CARD_COORDS["amount"]) or ""
    op = ctx.extract_at(*CARD_COORDS["operation_num"]) or ""
    if "RUR" not in amt.replace("\u00a0", " "):
        return False
    if not re.match(r"^Z09\d{13}", op.replace("\u00a0", "").strip()):
        return False
    if ctx.slot_size_at(*CARD_COORDS["date_time"]) < 20:
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
        dt_slot = ctx.slot_size_at(*CARD_COORDS["date_time"])
        op_slot = ctx.slot_size_at(*CARD_COORDS["operation_num"])
        scored.append((1 if ok_g else 0, dt_slot, op_slot, p))
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    out = [p for _, _, _, p in scored]
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


def _verify_committed(pdf: bytes, prepared: Dict[str, str]) -> bool:
    """После commit — текст в PDF должен совпадать с prepared."""
    ctx = AlfaOrigContext()
    if not ctx.load_bytes(pdf):
        logger.warning("Alfa CARD verify: load_bytes failed")
        return False
    for key in CARD_COORDS:
        if key not in prepared:
            continue
        got = _normalize_field(ctx.extract_at(*CARD_COORDS[key]))
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


def _attempt(path: str, data: Dict, *, tag: str, max_trials: int = 5) -> Optional[bytes]:
    """Поля + Java Deflater(6) ровно в /Length донора (без xref-rebuild и без \\x00-pad).

    Rebuild Length ломает чужие чекеры; pad → ALFA_MIXED_ZLIB у Proton.
    Крутим operation_num, пока compressed size не совпадёт 1:1.
    """
    from alfa_orig_mode import _oracle_near_flate
    from alfa_sbp_stealth import _is_auto_token

    base = dict(data)
    auto_operation = _is_auto_token(base.get("operation_num") or base.get("operation_number"))
    overshoot_streak = 0
    for trial in range(max(1, max_trials)):
        ctx = AlfaOrigContext()
        if not ctx.load(path):
            return None
        prepared = _prepare_card(
            base if trial == 0 or not auto_operation else {**base, "operation_num": "авто"},
            ctx=ctx,
        )

        slot_failed = False
        for key, (y, x) in CARD_COORDS.items():
            if key not in prepared:
                continue
            need = len(prepared[key].rstrip(_NBSP))
            if "RUR" in prepared[key].replace(_NBSP, " "):
                need += 1
            have = ctx.slot_size_at(y, x)
            if have <= 0 or need <= have:
                continue
            if not ctx.grow_slot_at(y, x, need):
                logger.info(
                    "[%s %s] slot grow failed %s need=%d have=%d",
                    tag, os.path.basename(path), key, need, have,
                )
                slot_failed = True
                break
        if slot_failed:
            continue
        for key in _CARD_MONEY_KEYS:
            if key not in prepared:
                continue
            if "RUR" not in prepared[key].replace(_NBSP, " "):
                logger.info("[%s %s] amount lost RUR: %r", tag, os.path.basename(path), prepared[key])
                slot_failed = True
                break
        if slot_failed:
            continue

        ok, why = ctx.fits_fields(CARD_COORDS, prepared)
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
        for key, (y, x) in CARD_COORDS.items():
            if key in prepared and not ctx.replace_at(y, x, prepared[key]):
                logger.info("[%s %s] replace fail: %s", tag, os.path.basename(path), key)
                bad_replace = True
                break
        if bad_replace:
            continue

        stream_b = bytes(ctx.stream)
        comp = _oracle_near_flate(stream_b, ctx.zlib_level)
        if len(comp) != ctx.orig_comp_len:
            # Паддинг почти никогда не УМЕНЬШАЕТ flate — сразу новый op-num.
            if len(comp) > ctx.orig_comp_len:
                overshoot_streak += 1
                logger.info(
                    "[%s %s] flate %d>%d trial=%d — retry op",
                    tag,
                    os.path.basename(path),
                    len(comp),
                    ctx.orig_comp_len,
                    trial,
                )
                # Стойкий overshoot — сумма/слот, op не поможет.
                if overshoot_streak >= 3:
                    logger.warning(
                        "[%s %s] persistent flate> — Java rebuild (no zlib pad)",
                        tag, os.path.basename(path),
                    )
                    result = ctx.commit(force=True)
                    if result is not None:
                        result = _randomize_trailer_id(result)
                        from alfa_sbp_stealth import _finish_oracle_pdf

                        finished = _finish_oracle_pdf(
                            result, bytes(ctx.stream), prepared, _verify_committed, tag=tag,
                        )
                        if finished is not None:
                            logger.info(
                                "🔴 ALFA CARD %s: %d bytes (flate-rebuild ship)",
                                tag, len(finished),
                            )
                            return finished
                    continue
                continue
            overshoot_streak = 0
            from alfa_orig_mode import _nudge_stream_exact_flate

            nudged = _nudge_stream_exact_flate(
                stream_b, ctx.orig_comp_len, ctx.zlib_level,
            )
            if nudged is None:
                logger.info(
                    "[%s %s] flate %d≠%d trial=%d — retry op",
                    tag,
                    os.path.basename(path),
                    len(comp),
                    ctx.orig_comp_len,
                    trial,
                )
                continue
            ctx.stream = bytearray(nudged)
            stream_b = nudged
            comp = _oracle_near_flate(stream_b, ctx.zlib_level)
            if len(comp) != ctx.orig_comp_len:
                logger.info(
                    "[%s %s] flate nudge miss %d≠%d trial=%d",
                    tag,
                    os.path.basename(path),
                    len(comp),
                    ctx.orig_comp_len,
                    trial,
                )
                continue
            logger.info(
                "[%s %s] flate nudged OK trial=%d",
                tag, os.path.basename(path), trial,
            )

        overshoot_streak = 0
        # in-place: commit попадёт в ветку exact length
        result = ctx.commit()
        if result is None:
            continue
        ctx_chk = AlfaOrigContext()
        if not ctx_chk.load_bytes(result) or ctx_chk.orig_comp_len != ctx.orig_comp_len:
            logger.info("[%s %s] Length drift after commit — retry", tag, os.path.basename(path))
            continue

        result = _randomize_trailer_id(result)
        if not _verify_committed(result, prepared):
            logger.info("[%s %s] post-commit verify failed", tag, os.path.basename(path))
            continue
        from alfa_sbp_stealth import _finish_oracle_pdf

        finished = _finish_oracle_pdf(
            result, bytes(ctx.stream), prepared, _verify_committed, tag=tag,
        )
        if finished is None:
            logger.info("[%s %s] oracle finish failed", tag, os.path.basename(path))
            continue
        logger.info("🔴 ALFA CARD %s: %d bytes (trial %d, exact flate)", tag, len(finished), trial)
        return finished

    logger.warning("[%s %s] no exact flate after retries — force last", tag, os.path.basename(path))
    ctx = AlfaOrigContext()
    if ctx.load(path):
        prepared = _prepare_card(base, ctx=ctx)
        slot_ok = True
        for key, (y, x) in CARD_COORDS.items():
            if key not in prepared:
                continue
            if not ctx.ensure_slot_at(y, x, prepared[key]):
                slot_ok = False
                break
            if not ctx.replace_at(y, x, prepared[key]):
                slot_ok = False
                break
        if slot_ok:
            result = ctx.commit(force=True)
            if result is not None:
                result = _randomize_trailer_id(result)
                from alfa_sbp_stealth import _finish_oracle_pdf

                finished = _finish_oracle_pdf(
                    result, bytes(ctx.stream), prepared, _verify_committed, tag=tag,
                )
                if finished is not None:
                    logger.info(
                        "🔴 ALFA CARD %s: %d bytes (final force ship)",
                        tag, len(finished),
                    )
                    return finished
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


def _gen_mir_pan(*, last4: Optional[str] = None, bin6: Optional[str] = None) -> str:
    """Корпус card: MIR 2200xx******xxxx. Никогда не оставляем слот пустым."""
    import secrets

    if not (bin6 and bin6.isdigit() and len(bin6) == 6 and 220_000 <= int(bin6) <= 220_499):
        bin6 = f"220{secrets.randbelow(500):03d}"
    if not (last4 and last4.isdigit() and len(last4) == 4):
        last4 = f"{secrets.randbelow(10_000):04d}"
    return f"{bin6}******{last4}"


def _fmt_card(card: str) -> str:
    raw = (card or "").strip()
    if not raw or raw.lower() in ("авто", "auto", "-"):
        return _gen_mir_pan()
    compact = re.sub(r"[\s\u00a0]", "", raw)
    masked = re.fullmatch(r"(\d{6})\*{4,8}(\d{4})", compact)
    if masked:
        return f"{masked.group(1)}******{masked.group(2)}"
    d = re.sub(r"\D", "", raw)
    if len(d) >= 16:
        return f"{d[:6]}******{d[-4:]}"
    last4 = d[-4:].zfill(4) if d else None
    return _gen_mir_pan(last4=last4)


def _parse_dt(date_in: str) -> datetime:
    from alfa_sbp_stealth import _parse_dt as _sbp_parse

    return _sbp_parse(date_in)


def _fmt_datetime(date_in, *, with_seconds: bool = True) -> str:
    """Как SBP: строка или datetime — одни часы на оба поля чека."""
    from alfa_sbp_stealth import _fmt_datetime as _sbp_fmt

    return _sbp_fmt(date_in, with_seconds=with_seconds)


def _gen_card_op_num(dt: datetime) -> str:
    """CARD: Z09 + DDMMYY + 7 цифр (корпус).

    Формат ^Z09\\d{13}$, дата [3:9]=DDMMYY.
    НЕ вшивать HHMMSS — тот же принцип, что SBP (C16+DDMMYY+7).
    """
    import secrets

    date_part = dt.strftime("%d%m%y")
    forbidden = dt.strftime("%H%M%S")
    for _ in range(32):
        tail7 = f"{secrets.randbelow(10_000_000):07d}"
        if tail7[:6] != forbidden:
            return f"Z09{date_part}{tail7}"
    return f"Z09{date_part}{(int(forbidden) + 17) % 1_000_000:06d}{secrets.randbelow(10)}"


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
    date_in = str(data.get("date_time") or data.get("date") or "сейчас")
    op_dt = _parse_dt(date_in)
    op = str(data.get("operation_num") or data.get("operation_number") or "")
    if not op or op.lower() in ("авто", "auto", "-"):
        op = _gen_card_op_num(op_dt)
    out = {
        "date_formed": _fmt_datetime(op_dt, with_seconds=False),
        "amount": _fmt_amount(str(data.get("amount", "0"))),
        "commission": _fmt_commission(),
        "sender_card": _fmt_card(str(data.get("sender_card") or data.get("account", ""))) + _NBSP,
        "receiver_card": _fmt_card(str(data.get("receiver_card") or data.get("card", ""))),
        "date_time": _fmt_datetime(op_dt, with_seconds=True),
        "operation_num": op + _NBSP,
    }
    for key in ("sender_card", "receiver_card"):
        compact = re.sub(r"[\s\u00a0]", "", out[key])
        if not re.fullmatch(r"\d{6}\*{4,8}\d{4}", compact):
            out[key] = _gen_mir_pan() + (_NBSP if key == "sender_card" else "")
    return out


def create_alfa_card_stealth(data: Dict) -> Optional[bytes]:
    from alfa_font_extend import ensure_alfa_font_chars
    from alfa_sbp_stealth import _blocked_alfa_chars, _is_auto_token

    # Полный пул доноров — только NATIVE FontFile2 (LIB → OnlyPDF «не распознан»).
    pool = list(_iter_donors(data))
    if not pool:
        logger.error("Alfa CARD: нет доноров")
        return None

    for outer in range(3):
        work = dict(data)
        if outer and _is_auto_token(data.get("operation_num") or data.get("operation_number")):
            work["operation_num"] = "авто"
        for path in pool[:16]:
            ctx = AlfaOrigContext()
            if not ctx.load(path):
                continue
            # Keep user date («сейчас»/«авто»/explicit) — never overwrite with donor day.
            from alfa_sbp_stealth import _is_auto_datetime

            user_dt = str(work.get("date_time") or work.get("date") or "").strip()
            if not user_dt or _is_auto_datetime(user_dt):
                work["date_time"] = "сейчас"
            work["operation_num"] = work.get("operation_num") or "авто"
            if outer and _is_auto_token(data.get("operation_num") or data.get("operation_number")):
                work["operation_num"] = "авто"
            prepared = _prepare_card(work, ctx=ctx)
            work_path = ensure_alfa_font_chars(path, "".join(prepared.values()), pool)
            hit = _attempt(work_path or path, work, tag="NATIVE", max_trials=12)
            if hit:
                return hit
    logger.error("Alfa CARD: все пути не удались")
    return None


def check_text(text: str) -> list:
    import alfa_glyph_library as agl
    from alfa_corpus import canonical_paths
    from alfa_orig_mode import union_available_chars

    agl.ensure_library()
    chars = set(agl.available_chars())
    chars |= union_available_chars(canonical_paths("card"))
    if not chars:
        tpl = _ensure_template()
        from alfa_orig_mode import load_available_chars
        chars = load_available_chars(tpl) if tpl else set()
    from alfa_sbp_stealth import _BLOCKED_ALFA_CHARS

    return [
        c for c in text
        if c in _BLOCKED_ALFA_CHARS or (c not in chars and c not in " \t\n")
    ]
