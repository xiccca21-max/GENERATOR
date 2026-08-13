"""
ALFA SBP — donor-orig pipeline (как T-Bank SBP / Ozon SBP).
"""
import os
import re
import logging
import random
from datetime import datetime, timedelta

from time_msk import now_msk
from typing import Dict, Optional

from alfa_orig_mode import AlfaOrigContext

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
SBP_ORIG = os.path.join(_DIR, "templates", "Alfa_sbp_original.pdf")
SBP_UNLOCKED = os.path.join(_DIR, "templates", "Alfa_sbp_unlocked.pdf")
_CORPUS_SEED = os.path.join(
    os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "альфа", "pdf (1).pdf",
)

_NBSP = "\u00a0"
_ALFA_SBP_MESSAGE = f"Перевод{_NBSP}денежных{_NBSP}средств"
_BLOCKED_ALFA_CHARS = frozenset()
_ALFA_SIZE_MIN = 58_000
_ALFA_SIZE_MAX = 59_200
_SENT_FF2_PATH = os.path.join(_DIR, "alfa_sbp_sent_ff2.txt")
_BANNED_FF2_SHA16 = frozenset({
    "c039bac6a70ea03f",  # five_01 PASS Ж
    "d59199cb81cdb664",  # five_02 PASS Ф
    "f8d54593242c08ea",
    "28f2257867b784bc",
    "c11f2b77d224e1a6",
    "2a7adfc0f1673432",
    "71bc88c6344b5e60",
    "b4464e3170ab4674",
    "716a8c9d3fd9c814",
    "4e1f19f328982027",  # padded Ж FAIL
    "24a1357bed119fe6",  # five_03 original-like FAIL
    "1c3b853481f80c79",  # five_04 pdf3+Щ FAIL
    "a601a4ec9ebd0223",  # five_06 pdf3+Й FAIL
    "1ea69f9bf21760db",  # five_07 pdf3+Ъ FAIL
    "cc60cf403f9b9cab",  # five_08 pdf3+З FAIL
    "2742d7c6977cd9d6",  # five_09 pdf3+О FAIL
    "0661fa982deac9b7",  # five_11 XUDZNH+ЗТ FAIL
    "cd0d778fe1f1fa57",  # padd_01 new-PADD clone of 014153 FAIL
    "7e7e3998d96fb845",  # accidental PADD on альфа банк сбп
    "635dee2685416c20",  # 014153 specimen FF2 — new face on it FAIL
})


def _corpus_ff2_shas() -> set:
    import hashlib
    import fitz
    cached = getattr(_corpus_ff2_shas, "_cache", None)
    if cached is not None:
        return cached
    from alfa_corpus import canonical_paths
    found: set = set()
    for path in list(canonical_paths("sbp") or []) + [SBP_ORIG]:
        if not path or not os.path.isfile(path):
            continue
        try:
            doc = fitz.open(path)
        except Exception:
            continue
        try:
            for xref in range(1, doc.xref_length()):
                try:
                    raw = doc.xref_stream(xref)
                except Exception:
                    continue
                if raw and raw[:4] == b"\x00\x01\x00\x00":
                    found.add(hashlib.sha256(raw).hexdigest()[:16])
                    break
        finally:
            doc.close()
    _corpus_ff2_shas._cache = found
    return found


def _ff2_sha16(pdf: bytes) -> str:
    import hashlib
    import fitz
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception:
        return ""
    try:
        for xref in range(1, doc.xref_length()):
            try:
                raw = doc.xref_stream(xref)
            except Exception:
                continue
            if raw and raw[:4] == b"\x00\x01\x00\x00":
                return hashlib.sha256(raw).hexdigest()[:16]
    finally:
        doc.close()
    return ""


def _sent_ff2_shas() -> set:
    found = set(_BANNED_FF2_SHA16)
    if os.path.isfile(_SENT_FF2_PATH):
        try:
            with open(_SENT_FF2_PATH, "r", encoding="utf-8") as fh:
                for line in fh:
                    token = line.strip().split()[0] if line.strip() else ""
                    if token:
                        found.add(token[:16])
        except OSError:
            pass
    return found


def _remember_ff2_sha(sha: str) -> None:
    if not sha or sha in _sent_ff2_shas():
        return
    try:
        with open(_SENT_FF2_PATH, "a", encoding="utf-8") as fh:
            fh.write(sha + "\n")
    except OSError:
        pass


def _corpus_subset_tags() -> set:
    cached = getattr(_corpus_subset_tags, "_cache", None)
    if cached is not None:
        return cached
    from alfa_corpus import canonical_paths

    tags = set()
    for path in list(canonical_paths("sbp") or []) + [SBP_ORIG]:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError:
            continue
        tags.update(re.findall(rb"/([A-Z]{6})\+Tahoma", blob))
    _corpus_subset_tags._cache = tags
    return tags


def _randomize_subset_tag(pdf: bytes, seed: bytes) -> bytes:
    """New 6-letter Oracle subset prefix. Same byte length — no xref rebuild."""
    import hashlib

    tags = set(re.findall(rb"/([A-Z]{6})\+Tahoma", pdf))
    if not tags:
        return pdf
    forbidden = set(tags) | _corpus_subset_tags()
    letters = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    new = b""
    for i in range(80):
        hh = hashlib.sha256(seed + bytes([i])).digest()
        cand = bytes(letters[hh[j] % 26] for j in range(6))
        if cand not in forbidden:
            new = cand
            break
    if not new:
        return pdf
    out = pdf
    for old in tags:
        out = out.replace(old + b"+Tahoma", new + b"+Tahoma")
    return out

# (y, x) — значения из корпуса
SBP_COORDS = {
    "date_formed": (779.15, 452.788),
    "amount": (664.3, 35.45),
    "commission": (621.4, 35.45),
    "date_time": (578.5, 35.45),
    "operation_num": (535.6, 35.45),
    "receiver": (492.7, 35.45),
    "phone": (664.3, 304.75),
    "recipient_bank": (621.4, 304.75),
    "account": (578.5, 304.75),
    "sbp_id": (535.6, 304.75),
    "message": (492.7, 304.75),
}

# Поля, которые НИКОГДА не заменяем donor-текстом.
_IDENTITY_KEYS = frozenset({
    "receiver", "recipient_bank", "phone", "account", "message",
    "amount", "commission", "sbp_id", "operation_num",
})


def _ensure_template() -> str:
    import shutil
    from alfa_corpus import rank_donors, has_compact_w_array

    ranked = rank_donors("sbp")
    src = ranked[0] if ranked else _CORPUS_SEED
    if not os.path.isfile(src):
        src = _CORPUS_SEED
    os.makedirs(os.path.dirname(SBP_ORIG), exist_ok=True)
    if os.path.isfile(SBP_ORIG):
        ctx = AlfaOrigContext()
        if (
            ctx.load(SBP_ORIG)
            and ctx.slot_size_at(621.4, 304.75) >= 16
            and has_compact_w_array(SBP_ORIG)
        ):
            return SBP_ORIG
    if src and os.path.isfile(src):
        shutil.copy2(src, SBP_ORIG)
    return SBP_ORIG if os.path.isfile(SBP_ORIG) else src


def _iter_donors(data: Dict) -> list:
    from alfa_corpus import canonical_paths
    from alfa_font_extend import _glyphs_ok_for_text

    auto_sbp = str(data.get("sbp_id") or "авто").strip().lower() in ("авто", "auto", "-", "")
    preview = _prepare_sbp(data)
    need_bank = len(str(preview.get("recipient_bank", "")).rstrip(_NBSP))
    need_recv = len(str(preview.get("receiver", "")).rstrip(_NBSP))
    scored = []
    for p in canonical_paths("sbp"):
        ctx = AlfaOrigContext()
        if not ctx.load(p):
            continue
        prepared = _prepare_sbp(data, ctx=ctx if auto_sbp else None)
        ok_g, _ = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, "".join(prepared.values()))
        bank_slot = ctx.slot_size_at(*SBP_COORDS["recipient_bank"])
        recv_slot = ctx.slot_size_at(*SBP_COORDS["receiver"])
        # Приоритет: charset → слоты уже вмещают ФИО/банк → размер слотов.
        fits = int(bank_slot >= need_bank) + int(recv_slot >= need_recv)
        scored.append((1 if ok_g else 0, fits, bank_slot, recv_slot, p))
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    out = [p for *_, p in scored]
    # Prefer lean originals (~58KB). Unlocked (~63KB) is last resort — live
    # Alfa SBP corpus sits at 58–59KB (user sees fat 62KB as «вес не как ориг»).
    lean = []
    fat = []
    for p in out:
        try:
            sz = os.path.getsize(p)
        except OSError:
            lean.append(p)
            continue
        (lean if 57_000 <= sz <= 60_500 else fat).append(p)
    prefs = [p for p in (_ensure_template(),) if p and os.path.isfile(p)]
    for pref in reversed(prefs):
        if pref in lean:
            lean.remove(pref)
        if pref in fat:
            fat.remove(pref)
        if 57_000 <= os.path.getsize(pref) <= 60_500:
            lean.insert(0, pref)
        else:
            fat.append(pref)
    unlocked = [p for p in (SBP_UNLOCKED,) if p and os.path.isfile(p)]
    for u in unlocked:
        if u in lean:
            lean.remove(u)
        if u in fat:
            fat.remove(u)
    return lean + fat + unlocked


def _normalize_field(s: str) -> str:
    return (s or "").replace(_NBSP, " ").strip()


def _blocked_alfa_chars(data: Dict) -> list:
    return sorted({
        ch
        for value in data.values()
        for ch in str(value or "")
        if ch in _BLOCKED_ALFA_CHARS
    })


def _verify_committed(pdf: bytes, prepared: Dict[str, str]) -> bool:
    """После commit — текст в PDF должен совпадать с prepared (ловит М→0 и т.п.)."""
    ctx = AlfaOrigContext()
    if not ctx.load_bytes(pdf):
        logger.warning("Alfa SBP verify: load_bytes failed")
        return False
    for key in SBP_COORDS:
        if key not in prepared:
            continue
        got = _normalize_field(ctx.extract_at(*SBP_COORDS[key]))
        want = _normalize_field(prepared[key])
        if got != want:
            logger.warning(
                "Alfa SBP verify mismatch %s: got=%r want=%r",
                key,
                got,
                want,
            )
            return False
    return True


def _fit_alfa_fontfile_size(
    pdf: bytes,
    content_stream: bytes,
    *,
    lean: bool = False,
    target: int = 58_400,
) -> Optional[bytes]:
    """Fit the shipped PDF through FontFile2 only.

    Phone's Quartz shell spends about 9 KB more than the Oracle shells on its
    unchanged image streams. All methods remove only TrueType hint programs,
    retaining CID positions, Unicode mappings and every contour; an
    incompressible decoded PADD table then lands safely inside the corpus band.
    """
    if _ALFA_SIZE_MIN <= len(pdf) <= _ALFA_SIZE_MAX:
        return pdf

    import hashlib
    from io import BytesIO

    from fontTools.ttLib import TTFont, newTable
    from fontTools.ttLib.tables.ttProgram import Program

    import alfa_font_extend as afe

    if lean:
        # The Quartz phone donor embeds the same three decoded Alfa image
        # assets as the Oracle receipts, but with streams 8–9 KB larger.
        # Reuse the shortest shipped original's compressed bytes by decoded
        # hash; image objects, references, dimensions and pixels stay intact.
        import fitz

        compact_by_hash = {}
        for donor_path in (SBP_ORIG,):
            if not os.path.isfile(donor_path):
                continue
            donor_doc = fitz.open(donor_path)
            try:
                for xref in range(1, donor_doc.xref_length()):
                    if "/Subtype /Image" not in donor_doc.xref_object(xref):
                        continue
                    decoded = donor_doc.xref_stream(xref)
                    raw = donor_doc.xref_stream_raw(xref)
                    key = hashlib.sha256(decoded).digest()
                    old = compact_by_hash.get(key)
                    if old is None or len(raw) < len(old):
                        compact_by_hash[key] = raw
            finally:
                donor_doc.close()
        target_doc = fitz.open(stream=pdf, filetype="pdf")
        replacements = []
        try:
            for xref in range(1, target_doc.xref_length()):
                if "/Subtype /Image" not in target_doc.xref_object(xref):
                    continue
                decoded = target_doc.xref_stream(xref)
                current_raw = target_doc.xref_stream_raw(xref)
                compact = compact_by_hash.get(hashlib.sha256(decoded).digest())
                if compact is not None and len(compact) < len(current_raw):
                    replacements.append((xref, compact))
        finally:
            target_doc.close()
        compact_pdf = bytearray(pdf)
        for xref, compact in replacements:
            pos = afe._find_stream_pos_for_xref(bytes(compact_pdf), xref)
            if not pos:
                return None
            patched = afe._patch_length_and_rebuild(
                compact_pdf, pos[0], pos[1], compact,
            )
            if patched is None:
                return None
            compact_pdf[:] = patched
        pdf = bytes(compact_pdf)

    refs = afe._load_font_xrefs_from_bytes(pdf)
    if not refs:
        return None
    try:
        original_ttf = afe._ff2_read_decompressed(pdf, refs["ff2"])
        font = TTFont(BytesIO(original_ttf))
        template = TTFont(BytesIO(original_ttf))
        glyph_order = font.getGlyphOrder()
    except Exception:
        return None

    if lean:
        for name in glyph_order:
            glyph = font["glyf"][name]
            if hasattr(glyph, "program"):
                glyph.program = Program()
                glyph.program.fromBytecode([])
        # PDF rendering uses the preserved outlines and widths. Removing
        # TrueType hint programs makes the exact full-charset font lean without
        # changing any CID position, Unicode mapping, or glyph contour.
        for tag in ("fpgm", "prep", "cvt "):
            if tag in font:
                del font[tag]
        base_ttf = afe._save_oracle_ttf(font, template)
    else:
        base_ttf = original_ttf

    seed = hashlib.sha256(pdf + b"ALFA-FONTFILE2-SIZE").digest()

    def entropy_tail(length: int) -> bytes:
        chunks = []
        counter = 0
        remaining = max(0, length)
        while remaining:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            take = min(remaining, len(block))
            chunks.append(block[:take])
            remaining -= take
            counter += 1
        return b"".join(chunks)

    def padded_ttf(length: int) -> bytes:
        if length <= 0:
            return base_ttf
        padded = TTFont(BytesIO(base_ttf))
        padded_template = TTFont(BytesIO(base_ttf))
        table = newTable("PADD")
        table.data = entropy_tail(length)
        padded["PADD"] = table
        return afe._save_oracle_ttf(padded, padded_template)

    # Rebuild from the same input each time; the size response is essentially
    # linear because the decoded tail is intentionally incompressible.
    tail_len = 0
    best = None
    for _ in range(10):
        candidate_pdf = bytearray(pdf)
        candidate_ttf = padded_ttf(tail_len)
        if not afe._patch_ff2_decompressed(candidate_pdf, refs["ff2"], candidate_ttf):
            return None
        candidate = bytes(candidate_pdf)
        if best is None or abs(len(candidate) - target) < abs(len(best) - target):
            best = candidate
        if _ALFA_SIZE_MIN <= len(candidate) <= _ALFA_SIZE_MAX:
            return candidate
        tail_len = max(0, tail_len + target - len(candidate))
    return best if best and _ALFA_SIZE_MIN <= len(best) <= _ALFA_SIZE_MAX else None


def _attempt(path: str, data: Dict, *, tag: str) -> Optional[bytes]:
    """Полные ФИО/банк (grow слота), exact Java flate = donor /Length."""
    from alfa_orig_mode import _oracle_near_flate

    base = dict(data)
    # Канонический текст пользователя — без обрезки.
    identity_want = {
        k: v
        for k, v in _prepare_sbp(base).items()
        if k in _IDENTITY_KEYS
    }
    if _is_auto_token(base.get("operation_num") or base.get("operation_number")):
        identity_want.pop("operation_num", None)
    if _is_auto_token(base.get("sbp_id") or base.get("spb_number")):
        identity_want.pop("sbp_id", None)

    for trial in range(24):
        ctx = AlfaOrigContext()
        if not ctx.load(path):
            return None
        trial_data = dict(base)
        if trial > 0 and _is_auto_token(base.get("operation_num") or base.get("operation_number")):
            trial_data["operation_num"] = "авто"
        if trial > 0 and _is_auto_token(base.get("sbp_id") or base.get("spb_number")):
            trial_data["sbp_id"] = "авто"
            # Слегка меняем date_formed через «сейчас» нельзя — меняем секунды op/sbp.
            if trial % 3 == 1 and "date_time" in trial_data:
                # перегенерить sbp/op уже; для flate — другой account pad не трогаем
                pass
        auto_sbp = str(trial_data.get("sbp_id") or "авто").strip().lower() in (
            "авто", "auto", "-", "",
        )
        prepared = _prepare_sbp(trial_data, ctx=ctx if auto_sbp else None)
        # Жёстко восстанавливаем identity из ввода пользователя.
        prepared.update(identity_want)

        # A short donor slot is expanded. If expansion is impossible, reject it;
        # accepted payload is never shortened or substituted.
        slot_failed = False
        for key, (y, x) in SBP_COORDS.items():
            if key not in prepared:
                continue
            need = len(prepared[key].rstrip(_NBSP))
            have = ctx.slot_size_at(y, x)
            if have <= 0:
                continue
            if need <= have:
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

        ok, why = ctx.fits_fields(SBP_COORDS, prepared)
        if not ok:
            logger.info("[%s %s] skip fit: %s", tag, os.path.basename(path), why)
            continue

        bad_replace = False
        for key, (y, x) in SBP_COORDS.items():
            if key not in prepared:
                continue
            if not ctx.replace_at(y, x, prepared[key]):
                logger.info("[%s %s] replace fail: %s", tag, os.path.basename(path), key)
                bad_replace = True
                break
        if bad_replace:
            continue

        drift = len(ctx.stream) - ctx.orig_dec_len
        if abs(drift) > 24:
            logger.info(
                "[%s %s] decoded drift %+d trial=%d — skip",
                tag, os.path.basename(path), drift, trial,
            )
            continue

        stream_b = bytes(ctx.stream)
        if b"%" in stream_b:
            logger.info("[%s %s] dirty CS %% — skip", tag, os.path.basename(path))
            continue
        comp = _oracle_near_flate(stream_b, ctx.zlib_level)
        if len(comp) != ctx.orig_comp_len:
            logger.info(
                "[%s %s] flate %d≠%d trial=%d — reject (no CS pad)",
                tag, os.path.basename(path), len(comp), ctx.orig_comp_len, trial,
            )
            continue

        result = ctx.commit()
        if result is None:
            continue
        ctx_chk = AlfaOrigContext()
        if not ctx_chk.load_bytes(result) or ctx_chk.orig_comp_len != ctx.orig_comp_len:
            continue

        result = _randomize_trailer_id(result)
        if not _verify_committed(result, prepared):
            continue
        try:
            from alfa_font_extend import _closure_fix_alfa_font

            fixed = _closure_fix_alfa_font(bytearray(result), bytes(ctx.stream))
            if fixed and len(fixed) >= len(result) - 64:
                result = fixed
        except Exception as exc:
            logger.warning("[%s] closure_fix skip: %s", tag, exc)
        if not _verify_committed(result, prepared):
            continue
        if not (_ALFA_SIZE_MIN <= len(result) <= _ALFA_SIZE_MAX):
            logger.info(
                "[%s %s] size %d off band — skip (no hint-strip PADD)",
                tag, os.path.basename(path), len(result),
            )
            continue
        sha = _ff2_sha16(result)
        if sha in _corpus_ff2_shas() or sha in _sent_ff2_shas():
            logger.info("[%s %s] FontFile2 %s already used — skip", tag, os.path.basename(path), sha)
            continue
        logger.info("🔴 ALFA SBP %s: %d bytes (trial %d) ff2=%s", tag, len(result), trial, sha)
        return result

    logger.warning("[%s %s] no emit after retries", tag, os.path.basename(path))
    return None


def _fmt_amount(amount_raw: str, *, max_chars: int = 0) -> str:
    """Всегда с разрядами: «10 000 RUR », «532 649 RUR », «1 000 000 RUR ».

    max_chars>0 — только хвост из NBSP до длины слота; группировку не снимаем.
    """
    digits = re.sub(r"\D", "", amount_raw or "0")
    n = int(digits) if digits else 0
    grouped = f"{n:,}".replace(",", _NBSP)
    body = f"{grouped}{_NBSP}RUR{_NBSP}"
    if max_chars > 0 and len(body) < max_chars:
        return body + (_NBSP * (max_chars - len(body)))
    if max_chars > 0 and len(body) > max_chars:
        # Убрать только хвостовые NBSP; «RUR» и пробелы тысяч обязательны.
        bare = f"{grouped}{_NBSP}RUR"
        if len(bare) <= max_chars:
            return bare + (_NBSP * (max_chars - len(bare)))
    return body


def _fmt_commission(raw: str = "0") -> str:
    """Как в оригинале: «0 RUR » / «1 200 RUR »."""
    digits = re.sub(r"\D", "", raw or "0")
    n = int(digits) if digits else 0
    if n == 0:
        return f"0{_NBSP}RUR{_NBSP}"
    grouped = f"{n:,}".replace(",", _NBSP)
    return f"{grouped}{_NBSP}RUR{_NBSP}"


def _fmt_phone(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("8") and len(d) == 11:
        d = "7" + d[1:]
    if len(d) == 11 and d.startswith("7"):
        d = d[1:]
    if len(d) == 10:
        return f"+7{_NBSP}({d[0:3]}){_NBSP}{d[3:6]}-{d[6:8]}-{d[8:10]}"
    return phone


_AUTO_DT_TOKENS = frozenset({"сейчас", "now", "авто", "auto", "-", ""})


def _is_auto_datetime(date_in: str) -> bool:
    raw = (date_in or "").strip().lower()
    return raw in _AUTO_DT_TOKENS


def _is_auto_token(value) -> bool:
    return str(value or "").strip().lower() in ("авто", "auto", "-", "")


def _fmt_datetime(date_in, *, with_seconds: bool = True) -> str:
    if isinstance(date_in, datetime):
        dt = date_in
    else:
        raw = (date_in or "").strip()
        if _is_auto_datetime(raw):
            dt = now_msk()
        else:
            raw = raw.replace(",", " ")
            dt = None
            for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
                try:
                    dt = datetime.strptime(raw.split("мск")[0].strip(), fmt)
                    break
                except ValueError:
                    pass
            if dt is None:
                dt = now_msk()
    if with_seconds:
        return dt.strftime("%d.%m.%Y") + f"{_NBSP}{dt.strftime('%H:%M:%S')}{_NBSP}мск{_NBSP}"
    return dt.strftime("%d.%m.%Y") + f"{_NBSP}{dt.strftime('%H:%M')}{_NBSP}мск"


def _gen_sbp_id_for_ctx(ctx: AlfaOrigContext, date_in: str) -> str:
    """SBP ID: обновляем только календарь/время/ref [1:14], хвост донора [14:32] не трогаем.

    Иначе ALFA_SBP_LINKED_TUPLE_CONFLICT: corpus tuple (control/class/slot/bank5/suffix)
    аттестован только с конкретным route_marker[14].
    """
    from datetime import timedelta
    import hashlib
    import secrets

    old = ctx.extract_at(*SBP_COORDS["sbp_id"]).strip().replace(_NBSP, "")
    if len(old) < 20:
        old = "A61551545348731O0G10080011770901"

    allowed_letters = set()
    for u in range(65, 91):
        cid = ctx.uni_to_cid.get(u)
        if cid is not None and cid in ctx.widths:
            allowed_letters.add(chr(u))
    allowed_digits = set()
    for d in range(ord("0"), ord("9") + 1):
        cid = ctx.uni_to_cid.get(d)
        if cid is not None and cid in ctx.widths:
            allowed_digits.add(chr(d))
    if not allowed_letters:
        allowed_letters = {"A", "C", "G", "O"}
    if not allowed_digits:
        allowed_digits = set("0123456789")
    letter_fallback = "A" if "A" in allowed_letters else sorted(allowed_letters)[0]
    digit_fallback = "0" if "0" in allowed_digits else sorted(allowed_digits)[0]

    dt_msk = now_msk()
    raw = (date_in or "").strip()
    if not _is_auto_datetime(raw):
        # Generators use "DD.MM.YYYY, HH:MM" — comma must be accepted or we fall
        # back to now_msk() → ALFA_SBP_ID_CALENDAR/TIME_ORDER_CONFLICT.
        raw_norm = raw.split("мск")[0].strip().replace(",", " ").replace(_NBSP, " ")
        raw_norm = re.sub(r"\s+", " ", raw_norm)
        for fmt in (
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%d.%m.%Y",
        ):
            try:
                dt_msk = datetime.strptime(raw_norm, fmt)
                break
            except ValueError:
                pass
    # encoded_local = encoded_utc+3h must be ≤ completion and lag ≤ 3h5m.
    # Keep a small positive lag (1..120s) so encoded is not later than completion.
    lag_sec = secrets.randbelow(120) + 1
    dt_utc = dt_msk - timedelta(hours=3) - timedelta(seconds=lag_sec)
    doy = dt_utc.timetuple().tm_yday
    year_dig = dt_utc.year - 2020
    hm = hashlib.md5(secrets.token_bytes(8)).digest()
    ref3 = f"{hm[0] * 10 + (hm[1] % 10):03d}"[-3:]

    head = (
        f"{year_dig * 1000 + doy:04d}"
        f"{dt_utc.hour:02d}{dt_utc.minute:02d}{dt_utc.second:02d}"
        f"{ref3}"
    )  # 4+6+3 = 13 chars → positions [1:14]

    out = list(old[:32].ljust(32, "0"))
    # [0] lead A/B
    if out[0] not in ("A", "B") or out[0] not in allowed_letters:
        out[0] = "A" if "A" in allowed_letters else letter_fallback
    # [1:14] only
    for i, ch in enumerate(head):
        pos = 1 + i
        if pos >= 14:
            break
        if ch.isdigit():
            out[pos] = ch if ch in allowed_digits else digit_fallback
        elif ch.isalpha():
            out[pos] = ch if ch in allowed_letters else letter_fallback
        else:
            out[pos] = digit_fallback
    # [14:32] — как у донора (route_marker + control/class/slot/bank/suffix)
    for i in range(14, min(32, len(out))):
        ch = out[i]
        if ch.isdigit() and ch not in allowed_digits:
            out[i] = digit_fallback
        elif ch.isalpha() and ch not in allowed_letters:
            out[i] = letter_fallback
    return "".join(out[:32])


def _parse_dt(date_in: str) -> datetime:
    raw = (date_in or "").strip()
    if _is_auto_datetime(raw):
        return now_msk()
    raw = raw.replace(",", " ")
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw.split("мск")[0].strip(), fmt)
        except ValueError:
            continue
    return now_msk()


def _gen_sbp_op_num(dt: datetime) -> str:
    """SBP: C16 + DDMMYY + 7 цифр (корпус).

    Формат ^C16\\d{13}$, дата [3:9]=DDMMYY.
    НЕ вшивать HHMMSS — иначе ALFA_KNOWN_GENERATOR_OPERATION_TIME_EMBEDDING.
    """
    import secrets

    date_part = dt.strftime("%d%m%y")
    forbidden = dt.strftime("%H%M%S")
    for _ in range(32):
        tail7 = f"{secrets.randbelow(10_000_000):07d}"
        if tail7[:6] != forbidden:
            return f"C16{date_part}{tail7}"
    # крайне редко: сдвинуть на 1с от forbidden
    return f"C16{date_part}{(int(forbidden) + 17) % 1_000_000:06d}{secrets.randbelow(10)}"


def _randomize_trailer_id(pdf: bytes) -> bytes:
    """Новый /ID — иначе ALFA_TRAILER_ID_REUSED_WITH_DIFFERENT_CONTENT."""
    import random

    rid = f"{random.getrandbits(128):032x}".encode("ascii")
    return re.sub(
        rb"/ID\s*\[\s*<[0-9A-Fa-f]{32}>\s*<[0-9A-Fa-f]{32}>\s*\]",
        b"/ID [<" + rid + b"><" + rid + b">]",
        pdf,
        count=1,
    )


def _prepare_sbp(data: Dict, *, ctx: Optional[AlfaOrigContext] = None) -> Dict[str, str]:
    date_in = str(data.get("date_time") or data.get("date") or "сейчас")
    # One MSK clock for both faces (date_formed + date_time). Never re-parse
    # «сейчас» twice and never add a random minute skew — that made the two
    # times disagree on the same receipt.
    op_dt = _parse_dt(date_in)
    date_key = op_dt.strftime("%d.%m.%Y %H:%M:%S")
    sbp_raw = str(data.get("sbp_id") or data.get("spb_number") or "авто")
    if _is_auto_token(sbp_raw):
        if ctx is not None:
            sbp_raw = _gen_sbp_id_for_ctx(ctx, date_key)
        else:
            sbp_raw = "A61551545348731O0G10080011770901"
    op = str(data.get("operation_num") or data.get("operation_number") or "")
    if not op or op.lower() in ("авто", "auto", "-"):
        op = _gen_sbp_op_num(op_dt)
    acct_raw = str(data.get("account", ""))
    if _is_auto_token(acct_raw):
        import secrets

        acct = f"408178{secrets.randbelow(10**10):010d}{secrets.randbelow(10_000):04d}"
    else:
        acct = acct_raw
    return {
        "date_formed": _fmt_datetime(op_dt, with_seconds=False),
        "amount": _fmt_amount(str(data.get("amount", "0"))),
        "commission": _fmt_commission(),
        "date_time": _fmt_datetime(op_dt, with_seconds=True),
        "operation_num": op + _NBSP,
        "receiver": str(data.get("receiver", "")),
        "phone": _fmt_phone(str(data.get("phone", ""))),
        "recipient_bank": str(data.get("recipient_bank", "Сбербанк")),
        "account": acct,
        "sbp_id": sbp_raw,
        "message": _ALFA_SBP_MESSAGE,
    }


def _extract_sbp_fields(path: str) -> Optional[Dict[str, str]]:
    ctx = AlfaOrigContext()
    if not ctx.load(path):
        return None
    out = {}
    for k, (y, x) in SBP_COORDS.items():
        v = ctx.extract_at(y, x).replace(_NBSP, " ").strip()
        if v:
            out[k] = ctx.extract_at(y, x)
    return out if len(out) >= 8 else None


def create_alfa_sbp_stealth(data: Dict) -> Optional[bytes]:
    from alfa_font_extend import _glyphs_ok_for_text, _donor_glyph_score

    blocked = _blocked_alfa_chars(data)
    if blocked:
        logger.error("Alfa SBP: unsupported exact chars: %s", "".join(blocked))
        return None

    raw_pool = list(_iter_donors(data))
    auto_sbp = str(data.get("sbp_id") or "авто").strip().lower() in ("авто", "auto", "-", "")

    def _score(path: str) -> int:
        ctx = AlfaOrigContext()
        if not ctx.load(path):
            return -1
        prepared = _prepare_sbp(data, ctx=ctx if auto_sbp else None)
        return _donor_glyph_score(path, "".join(prepared.values()))

    pool = sorted(raw_pool, key=_score, reverse=True)
    # Больше доноров: только NATIVE, нужен уже полный charset.
    pool_fast = pool[:24]
    if not pool_fast:
        logger.error("Alfa SBP: нет доноров")
        return None

    for outer in range(2):
        work = dict(data)
        if outer and _is_auto_token(data.get("operation_num") or data.get("operation_number")):
            work["operation_num"] = "авто"
        if outer and _is_auto_token(data.get("sbp_id") or data.get("spb_number")):
            work["sbp_id"] = "авто"

        # 1) Lean corpus first (~58KB). Inject missing glyphs into a lean shell
        # before falling back to fat unlocked (~63KB).
        from alfa_corpus import canonical_paths
        from alfa_font_extend import ensure_alfa_font_chars

        pdf3 = next(
            (p for p in canonical_paths("sbp") if p and os.path.basename(p) == "pdf (3).pdf"),
            None,
        )
        lean_pool = [
            p for p in pool_fast
            if os.path.isfile(p) and 57_000 <= os.path.getsize(p) <= 60_500
        ] or list(pool_fast)
        if pdf3 and pdf3 in lean_pool:
            lean_pool = [pdf3] + [p for p in lean_pool if p != pdf3]
        try_paths: list = []
        for path in lean_pool[:8]:
            ctx0 = AlfaOrigContext()
            if not ctx0.load(path):
                continue
            preview = _prepare_sbp(work, ctx=ctx0)
            extended = ensure_alfa_font_chars(
                path, "".join(preview.values()), lean_pool + pool_fast,
            )
            try_paths.append(extended or path)
        # Dedupe preserving order; unlocked last.
        seen_p = set()
        ordered = []
        for p in try_paths + pool_fast:
            if not p or p in seen_p or not os.path.isfile(p):
                continue
            seen_p.add(p)
            ordered.append(p)

        for path in ordered:
            ctx = AlfaOrigContext()
            if not ctx.load(path):
                continue
            # Keep the user's date/time («сейчас»/«авто»/explicit). Never replace
            # with donor calendar — that left auto dates stuck on old donor day.
            work_path = dict(work)
            if outer and _is_auto_token(data.get("operation_num") or data.get("operation_number")):
                work_path["operation_num"] = "авто"
            if outer and _is_auto_token(data.get("sbp_id") or data.get("spb_number")):
                work_path["sbp_id"] = "авто"
            prepared = _prepare_sbp(work_path, ctx=ctx)
            hit = _attempt(path, work_path, tag="NATIVE")
            if hit:
                return hit

        logger.info(
            "Alfa SBP: NATIVE miss outer=%d (glyphs/slots)",
            outer,
        )

    logger.error("Alfa SBP: все пути не удались")
    return None


def check_text(text: str) -> list:
    import alfa_glyph_library as agl
    from alfa_corpus import canonical_paths
    from alfa_orig_mode import union_available_chars

    agl.ensure_library()
    chars = set(agl.available_chars())
    chars |= union_available_chars(canonical_paths("sbp"))
    if not chars:
        tpl = _ensure_template()
        from alfa_orig_mode import load_available_chars
        chars = load_available_chars(tpl) if tpl else set()
    return [
        c for c in text
        if c in _BLOCKED_ALFA_CHARS or (c not in chars and c not in " \t\n")
    ]
