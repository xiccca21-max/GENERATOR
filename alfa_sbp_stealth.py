"""
ALFA SBP — donor-orig pipeline (как T-Bank SBP / Ozon SBP).
"""
import hashlib
import os
import re
import logging
import random
from datetime import datetime, timedelta

from time_msk import now_msk
from typing import Dict, List, Optional

from alfa_orig_mode import AlfaOrigContext, cap_trailing_nbsp

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
_ALFA_SIZE_MIN = 56_000
# Lean Oracle ≤59087; Wildberries orig ~59.8–60.7 KB (see _onlypdf_size_ok).
_ALFA_LEAN_SIZE_MAX = 59_087
_ALFA_WB_SIZE_MIN = 59_800
_ALFA_WB_SIZE_MAX = 61_200
# Soft band only — never reject emit on size (natural subset / long FIO OK).
_ALFA_SIZE_MAX = 63_000
_SENT_FF2_PATH = os.path.join(_DIR, "alfa_sbp_sent_ff2.txt")
_SENT_PREFIX_PATH = os.path.join(_DIR, "alfa_sbp_sent_prefixes.txt")
# Full-file SHA of originals already shown to @bankpdfbot — do not reuse as donors.
_BURNED_PDF_SHA16 = frozenset({
    "0415ab08a8903360",  # pdf.pdf fresh ❌
    "d397a2349b30dad8",  # pdf (3).pdf «не распознан»
    "e290cf6c32f1df77",  # сбп альфа 2.pdf cached ✅
})
_PASS_FF2_SHA16 = frozenset({
    "c039bac6a70ea03f",  # five_01 pdf3 Ж@77 n=78
    "d59199cb81cdb664",  # five_02 pdf3 Ф@77 n=78
    "7dbac8563f3a5a4c",  # pass_zhf_01 pdf3 Ж@77 Ф@78 n=79
    "d492ba7632c04391",  # pass_a7_zh альфА7 Ж@77 n=78
    "905e7e15542f72fa",  # pass_s7_zh альфа сбп7 Ж@71 n=72
    "4e36a7253841ecaf",  # streak_03 pdf.pdf Ж@76 B101
    "5fdbeb7e86e84b5a",  # 05_fio_nbsp Документ(1)+Ж + FIO 000A
})
_BANNED_FF2_SHA16 = frozenset({
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
    "b84f5a986192e5a2",  # native_01 pdf(2) keep-FF2 FAIL
    "3e9be6c15bcf005c",  # native_02 Документ(1) keep-FF2 FAIL
    "4d49ee37ccc3d7f7",  # pass_zh_01 pdf3+Ж hmtx bump FAIL
    "afa85faad60198f1",  # pass_zh_p2 pdf2+Ж steal FAIL
    "d880810b890f505a",  # pass_zhf_02 uniquify of PASS dual FAIL
    "9eca8fde7b7f74bb",  # pass_fzh_01 Ф@77 Ж@78 FAIL
    "da1274551869802c",  # pass_zhf_n80 orphan clone FAIL
    "09cde8a7dc074e38",  # pass_zhx_01 Ж+Х FAIL
    "b7115ea22defe55e",  # pass_a7_zhf dual on a7 FAIL
    "59584ddff954c819",  # pass_p2_zh pdf(2)+Ж FAIL
    "2b1ba3b71400d140",  # pass_psb_zh альфа сбп ПСБ FAIL
    "f39a8a691daca5a9",  # pass_gx_zh GXWQKE FAIL
    "be8b734f584545e0",  # pass_s7_f s7+Ф@71 FAIL
    "e377e36da464590e",  # csa_zh_01 pdf1+Ж CSA pin FAIL
    "36288b0a6df32936",  # csa_f_01 pdf1+Ф CSA pin FAIL
    "b07f33960e9e67ea",  # mathcsa_zh_01 pdf1+Ж math CSA FAIL
    "100d315f3e7f99a0",  # streak_01 pdf4+Ж FAIL
    "6779bed307725b2c",  # streak_02 Документ+Ж FAIL
    "3968d1ab1783d08e",  # streak_04 pdf.pdf+Ф FAIL
    "5fdbeb7e86e84b5a",  # streak_05 Документ(1)+Ж B100 FAIL
    "8d6f99669e72a22d",  # streak_08 pdf.pdf orphan clone FAIL
    "b08c23f402fef382",  # streak_09 сбп альфа B+Ж FAIL
    "92cea13e13ecf143",  # streak_10 pdf.pdf+Ег from pdf2 FAIL
    "20472ce62eda0c5d",  # сбп альфа+Ж A101 unsent
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
    for kind in ("sbp", "card"):
        for path in list(canonical_paths(kind) or []):
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as fh:
                    blob = fh.read()
            except OSError:
                continue
            tags.update(re.findall(rb"/([A-Z]{6})\+Tahoma", blob))
    if os.path.isfile(SBP_ORIG):
        try:
            with open(SBP_ORIG, "rb") as fh:
                tags.update(re.findall(rb"/([A-Z]{6})\+Tahoma", fh.read()))
        except OSError:
            pass
    _corpus_subset_tags._cache = tags
    return tags


def _corpus_prefix_ff2_map() -> Dict[bytes, str]:
    """Corpus subset tag → FontFile2 sha16. Frozen tag + other FF2 is the catch."""
    cached = getattr(_corpus_prefix_ff2_map, "_cache", None)
    if cached is not None:
        return cached
    import hashlib

    from alfa_corpus import canonical_paths
    from alfa_font_extend import _ff2_read_decompressed, _load_font_xrefs_from_bytes

    out: Dict[bytes, str] = {}
    paths = []
    for kind in ("sbp", "card"):
        paths.extend(canonical_paths(kind) or [])
    if os.path.isfile(SBP_ORIG):
        paths.append(SBP_ORIG)
    seen = set()
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        try:
            blob = open(path, "rb").read()
        except OSError:
            continue
        tags = set(re.findall(rb"/([A-Z]{6})\+Tahoma", blob))
        if not tags:
            continue
        refs = _load_font_xrefs_from_bytes(blob)
        if not refs:
            continue
        ff2 = _ff2_read_decompressed(blob, refs["ff2"])
        if not ff2:
            continue
        sha = hashlib.sha256(ff2).hexdigest()[:16]
        for tag in tags:
            out[tag] = sha
    _corpus_prefix_ff2_map._cache = out
    return out


def _sent_prefix_map() -> Dict[bytes, str]:
    out: Dict[bytes, str] = {}
    if not os.path.isfile(_SENT_PREFIX_PATH):
        return out
    try:
        with open(_SENT_PREFIX_PATH, "r", encoding="ascii") as fh:
            for line in fh:
                parts = line.strip().split()
                if len(parts) >= 2 and re.fullmatch(r"[A-Z]{6}", parts[0]):
                    out[parts[0].encode("ascii")] = parts[1][:16]
    except OSError:
        pass
    return out


def _sent_cid_signatures() -> set:
    out = set()
    if not os.path.isfile(_SENT_PREFIX_PATH):
        return out
    try:
        with open(_SENT_PREFIX_PATH, "r", encoding="ascii") as fh:
            for line in fh:
                parts = line.strip().split()
                if len(parts) >= 3:
                    out.add(parts[2])
    except OSError:
        pass
    return out


def _cid_map_signature(ctx: AlfaOrigContext) -> str:
    raw = b"".join(
        int(cid).to_bytes(2, "big") + int(cp).to_bytes(4, "big")
        for cid, cp in sorted(ctx.cid_to_uni.items())
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _remember_prefix(prefix: bytes, ff2_sha: str, cid_signature: str = "") -> None:
    if not re.fullmatch(rb"[A-Z]{6}", prefix or b"") or not ff2_sha:
        return
    known = _sent_prefix_map()
    if prefix in known:
        if known[prefix] != ff2_sha[:16]:
            raise ValueError("subset prefix reused for different FontFile2")
        return
    with open(_SENT_PREFIX_PATH, "a", encoding="ascii") as fh:
        fh.write(
            f"{prefix.decode('ascii')} {ff2_sha[:16]} {cid_signature or '-'}\n"
        )

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
    from alfa_orig_mode import _need_len

    need_bank = len(str(preview.get("recipient_bank", "")).rstrip(_NBSP))
    need_recv = _need_len(str(preview.get("receiver", "")), key="receiver")
    scored = []
    for p in canonical_paths("sbp"):
        ctx = AlfaOrigContext()
        if not ctx.load(p):
            continue
        if not _oracle_sbp_shell_ok(p):
            continue
        donor_sbp = str(ctx.extract_at(*SBP_COORDS["sbp_id"]) or "").replace(_NBSP, "")
        prepared = _prepare_sbp(data, ctx=ctx if auto_sbp else None)
        ok_g, _ = _glyphs_ok_for_text(ctx, ctx.pdf_bytes, "".join(prepared.values()))
        bank_slot = ctx.slot_size_at(*SBP_COORDS["recipient_bank"])
        recv_slot = ctx.slot_size_at(*SBP_COORDS["receiver"])
        sbp = prepared.get("sbp_id") or donor_sbp
        sbp = str(sbp).replace(_NBSP, "")
        # 05_fio PASS: native B100. Do not skip G10x — 01/02 likely died on FIO NBSP.
        # Do not transplant B101 (streak_09 shift).
        route = 2 if "B101" in sbp else (1 if "B100" in sbp else 0)
        fits = int(bank_slot >= need_bank) + int(recv_slot >= need_recv)
        scored.append((1 if ok_g else 0, route, fits, recv_slot, bank_slot, p))
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]), reverse=True)
    out = [p for *_, p in scored]
    # 15.08 WB orig is 60601 — that is an original, not unlocked fat.
    # Cap 59087 is the 15.08 Ozon late orig; treating 60601 as last-resort
    # forced long FIO onto the 59087 shell → 59197 → OnlyPDF FAKE.
    lean = []
    fat = []
    for p in out:
        try:
            sz = os.path.getsize(p)
        except OSError:
            lean.append(p)
            continue
        (lean if 57_000 <= sz <= 61_000 else fat).append(p)
    prefs = []
    for pref in reversed(prefs):
        try:
            import hashlib

            with open(pref, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest()[:16] in _BURNED_PDF_SHA16:
                    continue
        except OSError:
            continue
        if pref in lean:
            lean.remove(pref)
        if pref in fat:
            fat.remove(pref)
        if 57_000 <= os.path.getsize(pref) <= 61_000:
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
    from alfa_oracle_master import missing_chars

    preview = _prepare_sbp(data)
    found = missing_chars("".join(preview.values()))
    for ch in "".join(preview.values()):
        if ch in _BLOCKED_FACE_LETTERS and ch not in found:
            found.append(ch)
    return found


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


def _fontfile2_has_exact_sfnt_end(pdf: bytes) -> bool:
    """Release gate: FontFile2 ends exactly at the final aligned SFNT table."""
    from alfa_emit import sfnt_has_exact_aligned_end
    from alfa_font_extend import _ff2_read_decompressed, _load_font_xrefs_from_bytes

    refs = _load_font_xrefs_from_bytes(pdf)
    if not refs:
        return False
    ttf = _ff2_read_decompressed(pdf, refs["ff2"])
    return bool(ttf) and sfnt_has_exact_aligned_end(ttf)


def _et_space_ok(stream: bytes, donor_stream: bytes) -> bool:
    """Genuine Oracle: count(' ET') is 0 or ≥30. Never introduce a lone ' ET'."""
    n = len(re.findall(rb" ET\b", stream))
    donor_n = len(re.findall(rb" ET\b", donor_stream))
    if n == donor_n:
        return True
    return n == 0 or n >= 30


def _nudge_spaces_before_nl_et(stream: bytes, target: int, level: int) -> Optional[bytes]:
    """Grow Java flate with spaces before \\nET — never ' ET' and never % comments."""
    from alfa_orig_mode import _oracle_near_flate

    donor_et = stream.count(b" ET")
    idxs = []
    for mk in (b"\r\nET", b"\nET"):
        i = 0
        while True:
            j = stream.find(mk, i)
            if j < 0:
                break
            idxs.append(j)
            i = j + 1
    if not idxs:
        return None
    for idx in reversed(idxs):
        lo, hi = 1, 420
        hit_n = None
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = stream[:idx] + (b" " * mid) + stream[idx:]
            clen = len(_oracle_near_flate(cand, level))
            if clen == target:
                hit_n = mid
                break
            if clen < target:
                lo = mid + 1
            else:
                hi = mid - 1
        start = max(1, (hit_n or hi) - 8)
        end = min(420, (hit_n or lo) + 40)
        for n in range(start, end + 1):
            cand = stream[:idx] + (b" " * n) + stream[idx:]
            if b"%" in cand or cand.count(b" ET") != donor_et:
                continue
            if len(_oracle_near_flate(cand, level)) == target:
                return cand
    return None


def _rur_trailing_nbsp_ok(ctx: AlfaOrigContext) -> bool:
    """Genuine RUR typography: exactly one trailing NBSP after `RUR`.

    Proton flags `ALFA_AMOUNT_TYPOGRAPHY_ANOMALY` when trailing NBSP is padded
    (e.g. `RUR\xa0\xa0`), even if at least one NBSP exists.
    """
    for key in ("amount", "commission"):
        got = ctx.extract_at(*SBP_COORDS[key])
        idx = got.find("RUR")
        if idx < 0:
            return False
        if got[idx + 3 : idx + 4] != _NBSP:
            return False
        # Disallow extra NBSP immediately after the required one.
        if len(got) > idx + 4 and got[idx + 4 : idx + 5] == _NBSP:
            return False
    return True


def _trailing_nbsp_cid_ok(ctx: AlfaOrigContext, key: str) -> bool:
    """Last painted CID of the slot must be U+00A0."""
    got = ctx.extract_at(*SBP_COORDS[key])
    if not got or not got.endswith(_NBSP):
        return False
    y, x = SBP_COORDS[key]
    slot = ctx._slot_at(y, x)
    if not slot:
        return False
    start, end, _n, _t = slot
    hx = bytes(ctx.stream[start:end]).decode("ascii")
    if len(hx) < 4:
        return False
    last = int(hx[-4:], 16)
    return ctx.cid_to_uni.get(last) == 0x00A0


def _fio_trailing_nbsp_ok(ctx: AlfaOrigContext) -> bool:
    """HARD: FIO ends with exactly one U+00A0 (origs / Deacon PASS)."""
    got = ctx.extract_at(*SBP_COORDS["receiver"]) or ""
    if not got.endswith(_NBSP):
        return False
    if len(got) >= 2 and got[-2] == _NBSP:
        return False
    return _trailing_nbsp_cid_ok(ctx, "receiver")


def _sbp_trailing_nbsp_ok(ctx: AlfaOrigContext) -> bool:
    """HARD: last painted CID of SBP id must be U+00A0."""
    return _trailing_nbsp_cid_ok(ctx, "sbp_id")


def _sbp_id_structure_ok(raw: str) -> bool:
    """Canonical: type | calendar/time | reference | control | channel | core | tail."""
    s = (raw or "").replace(_NBSP, "").strip().upper()
    if len(s) != 32 or s[0] not in "ABG":
        return False
    if not s[1:11].isdigit():
        return False
    if not re.fullmatch(r"[0-9A-Z]{6}", s[11:17]) or s[16] != "0":
        return False
    if not s[17].isalnum():
        return False
    return s[18:22].isdigit() and s[22:27].isdigit() and s[27:32].isdigit()


def _inject_face_library(path: str, prepared: Dict[str, str]) -> str:
    """Inject missing face chars from Oracle glyph library (no cross-PDF steal)."""
    from alfa_font_extend import inject_alfa_char
    from alfa_orig_mode import AlfaOrigContext, _slot_face_text

    working = path
    body = "".join(str(prepared.get(k, "")) for k in SBP_COORDS if k in prepared)
    for _round in range(64):
        ctx = AlfaOrigContext()
        if not ctx.load(working):
            return path
        missing: set = set()
        for key in SBP_COORDS:
            if key not in prepared:
                continue
            text = prepared[key]
            if key == "receiver":
                text = _slot_face_text(text, key=key)
            ok, miss = ctx.can_render(text)
            if not ok:
                missing.update(miss)
        if not missing:
            return working
        progressed = False
        for ch in sorted(missing, key=ord):
            out = inject_alfa_char(
                working, ch, None, face_text=body, prefer_append=True,
            )
            if out:
                if out != working:
                    progressed = True
                working = out
        if not progressed:
            break
    return working


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

    # Library inject — full Cyrillic face without cross-PDF glyph steal.
    try:
        path = _inject_face_library(path, _prepare_sbp(base))
    except Exception as exc:
        logger.warning("[%s] font inject skip: %s", tag, exc)

    n_trials = 8 if ("operation_num" in identity_want and "sbp_id" in identity_want) else 32
    for trial in range(n_trials):
        ctx = AlfaOrigContext()
        if not ctx.load(path):
            return None
        from alfa_font_extend import _collect_alfa_used_cids as _used_cids

        orig_used = _used_cids(bytes(ctx.stream))
        donor_stream = bytes(ctx.stream)
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

        if not ctx.rebalance_slots(SBP_COORDS, prepared):
            logger.info(
                "[%s %s] slot rebalance failed trial=%d",
                tag, os.path.basename(path), trial,
            )
            continue

        ok, why = ctx.fits_fields(SBP_COORDS, prepared)
        if not ok:
            logger.info("[%s %s] skip fit: %s", tag, os.path.basename(path), why)
            continue

        bad_replace = False
        for key, (y, x) in SBP_COORDS.items():
            if key not in prepared:
                continue
            if not ctx.replace_at(y, x, prepared[key], key=key):
                logger.info("[%s %s] replace fail: %s", tag, os.path.basename(path), key)
                bad_replace = True
                break
        if bad_replace:
            continue

        for _pad_i in range(12):
            stream_b = bytes(ctx.stream)
            if b"%" in stream_b:
                break
            comp = _oracle_near_flate(stream_b, ctx.zlib_level)
            if len(comp) <= ctx.orig_comp_len:
                break
            if not ctx.trim_spare_pad(SBP_COORDS, prepared):
                break

        drift = len(ctx.stream) - ctx.orig_dec_len
        if drift > 80:
            logger.info(
                "[%s %s] decoded drift %+d trial=%d — skip",
                tag, os.path.basename(path), drift, trial,
            )
            continue

        stream_b = bytes(ctx.stream)
        if b"%" in stream_b:
            logger.info("[%s %s] dirty CS %% — skip", tag, os.path.basename(path))
            continue

        if not _et_space_ok(stream_b, donor_stream):
            logger.info("[%s %s] ' ET' anomaly — skip", tag, os.path.basename(path))
            continue

        comp = _oracle_near_flate(stream_b, ctx.zlib_level)
        if len(comp) == ctx.orig_comp_len:
            result = ctx.commit()
        else:
            # 05_fio live PASS: natural Java Deflater(6) + /Length/xref/startxref.
            result = ctx.commit_rebuild()
        if result is None:
            continue
        ctx_chk = AlfaOrigContext()
        if not ctx_chk.load_bytes(result):
            continue

        result = _ensure_fresh_oracle_trailer_id(result)
        if result is None:
            logger.info("[%s %s] trailer /ID fresh-equal failed — skip", tag, os.path.basename(path))
            continue
        if not _verify_committed(result, prepared):
            continue
        try:
            from alfa_font_extend import _closure_fix_alfa_font, _collect_alfa_used_cids

            dropped = orig_used - _collect_alfa_used_cids(bytes(ctx.stream))
            if len(dropped) in (1, 2, 3):
                fixed = _closure_fix_alfa_font(
                    bytearray(result), bytes(ctx.stream), only_cids=dropped,
                )
                if fixed and len(fixed) >= len(result) - 64:
                    result = fixed
            else:
                logger.info(
                    "[%s %s] dropped CIDs %s — keep (FIO 000A is the HARD)",
                    tag, os.path.basename(path), sorted(dropped)[:12],
                )
        except Exception as exc:
            logger.warning("[%s] closure_fix skip: %s", tag, exc)
        if not _verify_committed(result, prepared):
            continue
        chk_rur = AlfaOrigContext()
        if not chk_rur.load_bytes(result) or not _rur_trailing_nbsp_ok(chk_rur):
            logger.info("[%s %s] RUR trailing NBSP missing — skip", tag, os.path.basename(path))
            continue
        if not _fio_trailing_nbsp_ok(chk_rur):
            logger.info("[%s %s] FIO trailing 000A missing — skip", tag, os.path.basename(path))
            continue
        sbp_got = chk_rur.extract_at(*SBP_COORDS["sbp_id"])
        if not _sbp_id_structure_ok(sbp_got):
            logger.info("[%s %s] SBP structure invalid %r — skip", tag, os.path.basename(path), sbp_got)
            continue
        from alfa_font_extend import (
            _ff2_read_decompressed,
            _load_font_xrefs_from_bytes,
            _ot_checksum_matches,
        )

        refs = _load_font_xrefs_from_bytes(result)
        landed_ff2 = _ff2_read_decompressed(result, refs["ff2"]) if refs else b""
        if not landed_ff2 or not _ot_checksum_matches(landed_ff2):
            logger.info("[%s %s] OT checksum mismatch — skip", tag, os.path.basename(path))
            continue
        from alfa_emit import emit_invariants as _emit_inv

        why_inv = _emit_inv(result, channel="sbp")
        if why_inv:
            logger.info("[%s %s] %s — skip", tag, os.path.basename(path), why_inv)
            continue
        if not (_ALFA_SIZE_MIN <= len(result) <= _ALFA_SIZE_MAX):
            logger.warning(
                "[%s %s] soft-ship size %d off band",
                tag, os.path.basename(path), len(result),
            )
        sha = _ff2_sha16(result)
        corpus = _corpus_ff2_shas()
        if sha in _PASS_FF2_SHA16 or sha in _BANNED_FF2_SHA16 or (
            sha in _sent_ff2_shas() and sha not in corpus
        ):
            logger.info(
                "[%s %s] FontFile2 %s already used — skip",
                tag, os.path.basename(path), sha,
            )
            continue
        if sha not in corpus:
            _remember_ff2_sha(sha)
        logger.info("🔴 ALFA SBP %s: %d bytes (trial %d) ff2=%s", tag, len(result), trial, sha)
        return result

    logger.warning("[%s %s] no emit after retries", tag, os.path.basename(path))
    return None


def _fmt_amount(amount_raw: str, *, max_chars: int = 0, grouped: bool = True) -> str:
    """Всегда с разрядами перед RUR: «10 000 RUR».

    Genuine Oracle 30/30: trailing NBSP exists in the donor.
    For SBP we let `match_cs=True` / slot growth land it exactly once.
    """
    digits = re.sub(r"\D", "", amount_raw or "0")
    n = int(digits) if digits else 0
    grouped = f"{n:,}".replace(",", _NBSP) if grouped else str(n)
    # Do not hard-add the last trailing NBSP after RUR here.
    # With match_cs=True the rewriter pads faces to donor CID counts
    # and Proton is sensitive to "RUR\xa0\xa0" (typography anomaly).
    # Keeping the trailing NBSP for the donor-aligner makes it land exactly once.
    body = f"{grouped}{_NBSP}RUR"
    if max_chars > 0 and len(body) < max_chars:
        # At most one trailing NBSP after RUR (ALFA_AMOUNT_TYPOGRAPHY_ANOMALY).
        extra = min(1, max_chars - len(body))
        return body + (_NBSP * extra)
    return body


def _fmt_commission(raw: str = "0") -> str:
    """Как в оригинале: «0 RUR » / «1 200 RUR »."""
    digits = re.sub(r"\D", "", raw or "0")
    n = int(digits) if digits else 0
    if n == 0:
        return f"0{_NBSP}RUR"
    grouped = f"{n:,}".replace(",", _NBSP)
    return f"{grouped}{_NBSP}RUR"


def _fmt_phone(phone: str, *, compact: int = 0) -> str:
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("8") and len(d) == 11:
        d = "7" + d[1:]
    if len(d) == 11 and d.startswith("7"):
        d = d[1:]
    if len(d) == 10:
        if d[0] != "9":
            d = "9" + d[1:]
        a, b, c, e = d[0:3], d[3:6], d[6:8], d[8:10]
        # compact 0 = orig «+7 (XXX) XXX-XX-XX» (18). 1→17, 2→16.
        # Used only to walk decoded CS off the 5155–5191 Deacon ridge.
        if compact >= 3:
            return f"+7({a}){b}-{c}{e}"
        if compact >= 2:
            return f"+7({a}){b}-{c}-{e}"
        if compact >= 1:
            return f"+7{_NBSP}({a}){b}-{c}-{e}"
        return f"+7{_NBSP}({a}){_NBSP}{b}-{c}-{e}"
    return phone


_AUTO_DT_TOKENS = frozenset({"сейчас", "now", "авто", "auto", "-", ""})


def _is_auto_datetime(date_in: str) -> bool:
    raw = (date_in or "").strip().lower()
    return raw in _AUTO_DT_TOKENS


def _is_auto_token(value) -> bool:
    return str(value or "").strip().lower() in ("авто", "auto", "-", "")


def _fmt_datetime(date_in, *, with_seconds: bool = True, msk_gap: bool = True) -> str:
    if isinstance(date_in, datetime):
        dt = date_in
    else:
        raw = (date_in or "").replace("\xa0", " ").replace(",", " ").strip()
        if _is_auto_datetime(raw):
            dt = now_msk()
        else:
            dt = None
            for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
                try:
                    dt = datetime.strptime(raw.split("мск")[0].strip(), fmt)
                    break
                except ValueError:
                    pass
            if dt is None:
                dt = now_msk()
    gap = _NBSP if msk_gap else ""
    if with_seconds:
        return dt.strftime("%d.%m.%Y") + f"{_NBSP}{dt.strftime('%H:%M:%S')}{gap}мск{_NBSP}"
    return dt.strftime("%d.%m.%Y") + f"{_NBSP}{dt.strftime('%H:%M')}{gap}мск"


_BANK_ROUTES = (
    # Live 10.08.2026 Oracle: B + route 1013 + core 00118 + tail 21301.
    # Frozen vs NEG 3a (docs/specimen_alfa_sbp_NEG_3a24266b.pdf): that live ❌
    # had matching op/SBP — do not retune T-Bank/Ozon routes from the FAIL.
    (("т-банк", "тинькофф", "tinkoff", "t-bank", "tbank"), "Т-Банк", "B", "B10130011821301"),
    (("сбер",), "Сбербанк", "B", "B10110011760501"),
    # Face always «Озон Банк (Ozon)» — June corpus origs and 15.08 orig.
    # Tail is date-split in _bank_route: 15.08+ uses G10120011830701.
    (("озон", "ozon"), "Озон Банк (Ozon)", "B", "B10130011790502"),
    (("псб", "промсвяз"), "ПСБ", "B", "G10170011770101"),  # orig «альфа сбп.pdf» B+G10170011770101
    # Live 15.08.2026 Oracle: Дмитрий → Wildberries, lag 21s, size 60601.
    (
        ("wildberries", "вайлдберриз", "вайлдберрис", "вайлд", "wb"),
        "Wildberries (Вайлдберриз Банк)",
        "B",
        "G10140011830701",
    ),
    (("втб", "vtb"), "ВТБ", "A", "G10080011770901"),
    (("россельхоз", "рсхб", "rshb"), "Россельхозбанк", "A", "G10070011791103"),
    (("совком",), "Совкомбанк", "B", "B10130011821301"),
    (("газпром",), "Газпромбанк", "B", "B10130011821301"),
    (("райф", "raiffeisen"), "Райффайзенбанк", "B", "B10130011821301"),
    (("яндекс", "yandex"), "Яндекс Банк", "B", "B10130011821301"),
    (("акбарс", "ак барс", "ak bars", "akbars"), "Ак Барс Банк", "B", "B10130011821301"),
    (("открыти",), "Банк Открытие", "B", "B10130011821301"),
    (("мтс банк", "мтс-банк"), "МТС Банк", "B", "B10130011821301"),
    (("почта",), "Почта Банк", "B", "B10130011821301"),
    (("уралсиб",), "Уралсиб", "B", "B10130011821301"),
    (("хоум", "home credit"), "Хоум Банк", "B", "B10130011821301"),
    (("отп",), "ОТП Банк", "B", "B10130011821301"),
    (("ренессанс",), "Ренессанс Банк", "B", "B10130011821301"),
    (("модуль",), "Модульбанк", "B", "B10130011821301"),
    (("бкс",), "БКС Банк", "B", "B10130011821301"),
    # Brand face is mixed Cyrillic+Latin «ЮMoney»; Latin M/e/y borrow Cyr outlines.
    (("юmoney", "yoomoney", "юмани", "yumoney"), "ЮМани", "B", "B10130011821301"),
)
# Unknown recipient bank: keep the typed face, use live 10.08 T-Bank channel
# (not Ozon G10120011830701 / WB G10140011830701). Known banks keep their own tails.
_FALLBACK_ROUTE = ("B", "B10130011821301")
# Fallback B1013 banks walk nearest era in _ERA_OK_TAILS. Never WB G1014.
_ERA_FALLBACK_FACES = frozenset({
    "Газпромбанк", "Райффайзенбанк", "Яндекс Банк", "Совкомбанк",
    "Ак Барс Банк", "Банк Открытие", "МТС Банк", "Почта Банк",
    "Уралсиб", "Хоум Банк", "ОТП Банк", "Ренессанс Банк",
    "Модульбанк", "БКС Банк", "ЮМани",
})
_ERA_OK_TAILS = frozenset({
    "B10130011821301",
    "B10130011790502",
    "G10120011830701",
})
_SBP_LIVE_CORE = "00118"
_SBP_LIVE_BANK5 = "21301"
_SBP_LIVE_ROUTE4 = "1013"
# 15.08.2026 genuine Ozon orig (Документ (6) / document15.08.26_late.pdf):
# Алина identity is ЧС on @bankpdfbot — harvest layout only, never clone face/ids.
# Route G1012 + core 00118 + bank5 30701 (same bank5 as WB, different channel).
_OZON_ROUTE_SINCE = datetime(2026, 8, 15)
_OZON_ROUTE_NEW = ("B", "G10120011830701")
# Fresh 01–02.09 originals from `чеки/альфа/Документ (9..11).pdf`:
# Сбербанк -> A+B10030011840301, Т-Банк -> A+B10200011840301,
# Совкомбанк -> A+B10080011840301.
_SEP_ROUTE_SINCE = datetime(2026, 9, 1)
_SEP_ROUTE_SBER = ("A", "B10030011840301")
_SEP_ROUTE_TBANK = ("A", "B10200011840301")
_SEP_ROUTE_SOVKOM = ("A", "B10080011840301")
# 10.08 T-Bank orig (`document10.08.26.pdf`). June T-Bank origs are A+G100x.
_TBANK_ROUTE_SINCE = datetime(2026, 8, 10)
_TBANK_ROUTE_OLD = ("A", "G10080011770901")  # pdf (3).pdf 15.06 afternoon
_ROUTE_ORIG_CACHE: Optional[list] = None
_CHS_PHONES = frozenset({"9274890391"})
_CHS_FIO = frozenset({"алина александровна а"})
_LIVE_ORACLE_SHELL = "document10.08.26.pdf"
_BLOCKED_FACE_LETTERS = frozenset()  # full Cyrillic via source-shared parent; never quarantine FIO
_SENT_ID_PATH = os.path.join(_DIR, "alfa_sbp_sent_identity.txt")
_SENT_OP_PATH = os.path.join(_DIR, "alfa_sbp_sent_ops.txt")
_SENT_PAYLOAD_PATH = os.path.join(_DIR, "alfa_sbp_sent_payloads.txt")


def _normalize_bank_face(raw: str) -> str:
    s = " ".join(str(raw or "").split())
    return _NBSP.join(s.split()) if s else ""


def _match_corpus_bank(bank: str):
    """Resolve face + SBP lead/tail. Any non-empty bank is allowed."""
    low = (bank or "").lower().replace("ё", "е")
    for keys, face, lead, tail in _BANK_ROUTES:
        if any(k in low for k in keys):
            return face, lead, tail
    face = _normalize_bank_face(bank)
    if not face:
        return None
    lead, tail = _FALLBACK_ROUTE
    return face, lead, tail


def _sbp_lead_tail_from_id(raw: str) -> tuple:
    s = (raw or "").replace(_NBSP, "").strip().upper()
    if len(s) != 32:
        return "", ""
    return s[0], s[-15:]


def _bank_routes_from_origs() -> list:
    """(date, canonical bank, lead, tail) harvested from valid SBP origs."""
    global _ROUTE_ORIG_CACHE
    if _ROUTE_ORIG_CACHE is not None:
        return _ROUTE_ORIG_CACHE
    out = []
    for path in _layout_shells():
        ctx = AlfaOrigContext()
        if not ctx.load(path):
            continue
        sbp = ctx.extract_at(*SBP_COORDS["sbp_id"]) or ""
        lead, tail = _sbp_lead_tail_from_id(sbp)
        if not lead or not tail:
            continue
        bank = _slot_bank(ctx.extract_at(*SBP_COORDS["recipient_bank"]))
        hit = _match_corpus_bank(bank)
        if hit:
            bank = hit[0]
        dt_raw = (ctx.extract_at(*SBP_COORDS["date_time"]) or "").replace(_NBSP, " ")
        try:
            dt = _parse_dt(dt_raw)
        except Exception:
            continue
        out.append((dt.replace(tzinfo=None), bank, lead, tail))
    _ROUTE_ORIG_CACHE = out
    return out


def _bank_route(bank: str, dt: Optional[datetime] = None) -> tuple:
    hit = _match_corpus_bank(bank)
    if not hit:
        return "", ""
    face, lead, tail = hit
    low = (bank or "").lower().replace("ё", "е")
    is_ozon = "озон" in low or "ozon" in low or (face or "").startswith("Озон")
    naive = dt.replace(tzinfo=None) if dt is not None else None
    # September cluster from fresh originals (01.09 / 02.09).
    if naive is not None and naive >= _SEP_ROUTE_SINCE:
        is_tbank = (face or "") == "Т-Банк" or "т-банк" in low or "тинькофф" in low
        is_sber = (face or "") == "Сбербанк" or "сбер" in low
        is_sovkom = (face or "") == "Совкомбанк" or "совком" in low
        is_gazprom = (face or "") == "Газпромбанк" or "газпром" in low
        if is_tbank:
            return _SEP_ROUTE_TBANK
        if is_sber:
            return _SEP_ROUTE_SBER
        if is_sovkom:
            return _SEP_ROUTE_SOVKOM
        if is_gazprom:
            return _SEP_ROUTE_TBANK
        if is_ozon:
            return _SEP_ROUTE_TBANK
        # 02.09 regressions: generic non-special banks were drifting to Ozon
        # G1012 era and failing in Deacon. Keep September profile unified.
        return _SEP_ROUTE_TBANK
    if is_ozon and naive is not None and naive >= _OZON_ROUTE_SINCE:
        return _OZON_ROUTE_NEW
    is_sber = (face or "") == "Сбербанк" or "сбер" in low
    # May Sber orig (`альфа сбп7.pdf`) is B+B10110011760501 — 30/30 PASS on 19.05.
    # Same tail on 15.08 / 02.09 is Deacon FAKE (alfa_sbp_161609 + live 15.08
    # probe B6227…B1011). 15.08 live era is Ozon G1012; Sber follows that from
    # 15.08 on, not the May channel.
    if is_sber:
        if naive is not None and naive >= _OZON_ROUTE_SINCE:
            return _OZON_ROUTE_NEW
        return lead, tail
    is_tbank = (face or "") == "Т-Банк" or "т-банк" in low or "тинькофф" in low
    # T-Bank only: June origs are A+G100x, 10.08+ are B1013. Do NOT apply this
    # abs-delta harvest to Сбер/ПСБ/ВТБ — June shells paint «Сбербанк» with a
    # T-Bank G1004 tail (`альфа банк сбп.pdf`). Bot «сейчас» 02.09 then shipped
    # A+G1004 (alfa_sbp_160914, Мужик/Сбер, Deacon FAKE). Live Sber PASS is
    # B+B10110011760501 (19.05 / 20.05 in the 30/30).
    if is_tbank and naive is not None:
        best = None
        best_delta = None
        for orig_dt, orig_bank, orig_lead, orig_tail in _bank_routes_from_origs():
            if orig_bank != face:
                continue
            delta = abs((orig_dt - naive).total_seconds())
            if best is None or delta < best_delta:
                best = (orig_lead, orig_tail)
                best_delta = delta
        if best is not None:
            return best
        if naive < _TBANK_ROUTE_SINCE:
            return _TBANK_ROUTE_OLD
        return lead, tail
    if (lead, tail) != _FALLBACK_ROUTE:
        return lead, tail
    if naive is not None:
        # Ак Барс / Открытие / … : nearest era among B1013 and Ozon G1012,
        # never WB G1014.
        prefer_b1013_only = (face or "") in _ERA_FALLBACK_FACES
        era = None
        era_delta = None
        for orig_dt, _orig_bank, orig_lead, orig_tail in _bank_routes_from_origs():
            if orig_tail not in _ERA_OK_TAILS:
                continue
            if prefer_b1013_only and orig_tail != "B10130011821301":
                continue
            delta = abs((orig_dt - naive).total_seconds())
            if era is None or delta < era_delta:
                era = (orig_lead, orig_tail)
                era_delta = delta
        if era is not None:
            return era
    return lead, tail


def _identity_key(prepared: Dict[str, str]) -> str:
    phone = re.sub(r"\D", "", prepared.get("phone") or "")
    if phone.startswith("7") and len(phone) == 11:
        phone = phone[1:]
    fio = _normalize_field(prepared.get("receiver") or "").lower()
    amt = re.sub(r"\D", "", prepared.get("amount") or "")
    return f"{phone}|{fio}|{amt}"


def _face_amount_key(prepared: Dict[str, str]) -> str:
    """OnlyPDF FAKEd same FIO+amount with a different phone (Дмитрий/1500)."""
    fio = _normalize_field(prepared.get("receiver") or "").lower()
    amt = re.sub(r"\D", "", prepared.get("amount") or "")
    return f"face|{fio}|{amt}"


def _load_identity_set() -> set:
    if not os.path.isfile(_SENT_ID_PATH):
        return set()
    try:
        with open(_SENT_ID_PATH, "r", encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}
    except OSError:
        return set()


def _identity_used(key: str) -> bool:
    return bool(key) and key in _load_identity_set()


def _identity_blocked(prepared: Dict[str, str]) -> bool:
    # Duplicate FIO+amount / phone is not a user-facing restriction.
    # Prefix/FF2/op uniqueness still happens inside emit retries.
    return False


def _remember_identity(key: str) -> None:
    if not key or _identity_used(key):
        return
    try:
        with open(_SENT_ID_PATH, "a", encoding="utf-8") as fh:
            fh.write(key + "\n")
    except OSError:
        pass


def _remember_prepared_identity(prepared: Dict[str, str]) -> None:
    _remember_identity(_identity_key(prepared))
    _remember_identity(_face_amount_key(prepared))
    op = re.sub(r"\s+", "", (prepared.get("operation_num") or "").replace(_NBSP, ""))
    if op:
        _remember_op(op)


def _load_op_set() -> set:
    if not os.path.isfile(_SENT_OP_PATH):
        return set()
    try:
        with open(_SENT_OP_PATH, "r", encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}
    except OSError:
        return set()


def _op_used(op: str) -> bool:
    s = re.sub(r"\s+", "", (op or "").replace(_NBSP, ""))
    return bool(s) and s in _load_op_set()


def _remember_op(op: str) -> None:
    s = re.sub(r"\s+", "", (op or "").replace(_NBSP, ""))
    if not s or _op_used(s):
        return
    try:
        with open(_SENT_OP_PATH, "a", encoding="utf-8") as fh:
            fh.write(s + "\n")
    except OSError:
        pass


def _normalized_payload_fields(prepared: Dict[str, str]) -> Dict[str, str]:
    phone = re.sub(r"\D", "", prepared.get("phone") or "")
    if phone.startswith("7") and len(phone) == 11:
        phone = phone[1:]
    out = {
        "phone": phone,
        "account": re.sub(r"\D", "", prepared.get("account") or ""),
        "operation_num": re.sub(
            r"\s+", "", (prepared.get("operation_num") or "").replace(_NBSP, "")
        ),
        "sbp_id": re.sub(
            r"\s+", "", (prepared.get("sbp_id") or "").replace(_NBSP, "")
        ).upper(),
    }
    # CARD face — PAN tails so trailer/op reuse tracking works across methods.
    sender = re.sub(r"[^\d*]", "", (prepared.get("sender_card") or "").replace(_NBSP, ""))
    receiver = re.sub(
        r"[^\d*]", "", (prepared.get("receiver_card") or "").replace(_NBSP, "")
    )
    if sender or receiver:
        out["sender_card"] = sender
        out["receiver_card"] = receiver
        out["amount"] = re.sub(r"\D", "", prepared.get("amount") or "")
    return out


def _card_identity_key(prepared: Dict[str, str]) -> str:
    sender = re.sub(r"\D", "", prepared.get("sender_card") or "")
    receiver = re.sub(r"\D", "", prepared.get("receiver_card") or "")
    amt = re.sub(r"\D", "", prepared.get("amount") or "")
    return f"card|{sender}|{receiver}|{amt}"


def _card_face_amount_key(prepared: Dict[str, str]) -> str:
    receiver = re.sub(r"\D", "", prepared.get("receiver_card") or "")
    amt = re.sub(r"\D", "", prepared.get("amount") or "")
    return f"cardface|{receiver}|{amt}"


def _card_identity_blocked(prepared: Dict[str, str]) -> bool:
    used = _load_identity_set()
    return (
        _card_identity_key(prepared) in used
        or _card_face_amount_key(prepared) in used
    )


def _remember_card_identity(prepared: Dict[str, str]) -> None:
    _remember_identity(_card_identity_key(prepared))
    _remember_identity(_card_face_amount_key(prepared))
    op = re.sub(r"\s+", "", (prepared.get("operation_num") or "").replace(_NBSP, ""))
    if op:
        _remember_op(op)


def _phone_identity_key(prepared: Dict[str, str]) -> str:
    phone = re.sub(r"\D", "", prepared.get("phone") or "")
    if phone.startswith("7") and len(phone) == 11:
        phone = phone[1:]
    fio = _normalize_field(prepared.get("receiver") or "").lower()
    amt = re.sub(r"\D", "", prepared.get("amount") or "")
    return f"phone|{phone}|{fio}|{amt}"


def _phone_face_amount_key(prepared: Dict[str, str]) -> str:
    fio = _normalize_field(prepared.get("receiver") or "").lower()
    amt = re.sub(r"\D", "", prepared.get("amount") or "")
    return f"phoneface|{fio}|{amt}"


def _phone_identity_blocked(prepared: Dict[str, str]) -> bool:
    used = _load_identity_set()
    return (
        _phone_identity_key(prepared) in used
        or _phone_face_amount_key(prepared) in used
    )


def _remember_phone_identity(prepared: Dict[str, str]) -> None:
    _remember_identity(_phone_identity_key(prepared))
    _remember_identity(_phone_face_amount_key(prepared))
    op = re.sub(r"\s+", "", (prepared.get("operation_num") or "").replace(_NBSP, ""))
    if op:
        _remember_op(op)


def _load_sent_payloads() -> list:
    import json

    out = []
    if not os.path.isfile(_SENT_PAYLOAD_PATH):
        return out
    try:
        with open(_SENT_PAYLOAD_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(item, dict):
                    out.append(item)
    except OSError:
        pass
    return out


def _payload_reuse_field(prepared: Dict[str, str]) -> str:
    current = _normalized_payload_fields(prepared)
    for old in _load_sent_payloads():
        for key, value in current.items():
            if value and value == str(old.get(key) or ""):
                return key
    return ""


def _pdf_trailer_ids(pdf: bytes) -> tuple:
    hit = re.search(
        rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", pdf
    )
    if not hit:
        return ()
    return tuple(part.decode("ascii").lower() for part in hit.groups())


def _corpus_pdf_ids() -> set:
    """Trailer /ID hexes from Alfa corpus + template originals (Oracle equal-pair).

    Mutated ships must never reuse these — same body + new /ID is the
    «номер документа не соответствует оригиналу» / clone tell; donor /ID
    on mutated content is ALFA_TRAILER_ID_REUSED.
    """
    cached = getattr(_corpus_pdf_ids, "_cache", None)
    if cached is not None:
        return cached
    found: set = set()
    paths: list = []
    try:
        from alfa_corpus import canonical_paths

        for kind in ("sbp", "card", "phone"):
            paths.extend(canonical_paths(kind) or [])
    except Exception:
        pass
    for name in (
        "Alfa_sbp_original.pdf",
        "Alfa_card_original.pdf",
        "Alfa_phone_original.pdf",
        "Alfa_statement_original.pdf",
    ):
        p = os.path.join(_DIR, "templates", name)
        if os.path.isfile(p):
            paths.append(p)
    if os.path.isfile(SBP_ORIG):
        paths.append(SBP_ORIG)
    seen_paths = set()
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        key = os.path.normcase(os.path.normpath(path))
        if key in seen_paths:
            continue
        seen_paths.add(key)
        try:
            with open(path, "rb") as fh:
                found.update(_pdf_trailer_ids(fh.read()))
        except OSError:
            continue
    _corpus_pdf_ids._cache = found
    return found


def _sent_pdf_ids() -> set:
    return {
        str(item.get(key) or "").lower()
        for item in _load_sent_payloads()
        for key in ("pdf_id1", "pdf_id2")
        if item.get(key)
    }


def _oracle_trailer_ids_equal(pdf: bytes) -> bool:
    """Oracle SBP/phone/card originals: `/ID [<A><A>]` (equal pair)."""
    ids = _pdf_trailer_ids(pdf)
    return len(ids) == 2 and ids[0] == ids[1] and len(ids[0]) == 32


def _trailer_id_reused(pdf: bytes) -> bool:
    """True if /ID missing, unequal (non-Oracle), corpus, or already sent."""
    ids = _pdf_trailer_ids(pdf)
    if len(ids) != 2:
        return True
    if ids[0] != ids[1]:
        # Oracle receipts are equal-pair; unequal is iText/statement / bad clone.
        return True
    banned = _corpus_pdf_ids() | _sent_pdf_ids()
    return ids[0] in banned


def _oracle_trailer_invariant(pdf: bytes) -> str:
    """Empty if trailer /ID matches Oracle receipt rules (equal pair present)."""
    ids = _pdf_trailer_ids(pdf)
    if len(ids) != 2:
        return "trailer-id-missing"
    if ids[0] != ids[1]:
        return "trailer-id-unequal"
    if len(ids[0]) != 32:
        return "trailer-id-len"
    return ""


def _remember_sent_payload(
    prepared: Dict[str, str],
    pdf: bytes,
    *,
    prefix: bytes,
    ff2_sha: str,
    cid_signature: str,
) -> None:
    import json

    fields = _normalized_payload_fields(prepared)
    ids = _pdf_trailer_ids(pdf)
    fields.update({
        "pdf_id1": ids[0] if len(ids) == 2 else "",
        "pdf_id2": ids[1] if len(ids) == 2 else "",
        "prefix": prefix.decode("ascii"),
        "ff2_sha16": ff2_sha[:16],
        "cid_signature": cid_signature,
    })
    with open(_SENT_PAYLOAD_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fields, ensure_ascii=False, sort_keys=True) + "\n")


def alfa_sbp_reject_reason(data: Dict) -> Optional[str]:
    """User-facing why create would return None before emit."""
    bank_in = str(data.get("recipient_bank") or data.get("bank") or "")
    if not _match_corpus_bank(bank_in):
        return "❌ Укажите банк получателя."
    # Never reject rare Cyrillic — emit path paints from source-shared parent.
    return None


def _sbp_lag_bounds(dt_msk: datetime) -> tuple:
    """Live Oracle: encoded UTC = face − 3h.

    10.08 orig lag 5s; 15.08 Wildberries orig lag 21s. Do not retune from NEG 3a.

    date_formed is minute-only («19:49 мск» → Proton completion 19:49:00).
    Lag must be ≥ face seconds so encoded local is not later than that floor
    (19:49:41 > 19:49:00 → ALFA_SBP_ID_TIME_ORDER_CONFLICT / OnlyPDF FAKE).
    """
    sec = int(getattr(dt_msk, "second", 0) or 0)
    lo = max(3, sec)
    hi = max(lo, min(21, max(5, sec)))
    return lo, hi


def _sbp_utc_clock(dt_msk: datetime, lag_sec: int) -> datetime:
    """SBP ID stores UTC = (operation_datetime − lag) − 3 hours.

    Genuine 10.08.2026 10:48:15 → 07:48:10 UTC (lag 5s). When the face has
    seconds, lag ≥ those seconds so encoded local ≤ date_formed HH:MM:00.
    """
    min_lag, max_lag = _sbp_lag_bounds(dt_msk)
    lag = max(min_lag, min(max_lag, int(lag_sec)))
    op = dt_msk.replace(microsecond=0)
    return op - timedelta(hours=3) - timedelta(seconds=lag)


def _sbp_calendar_head(dt_utc: datetime, ref3: str) -> str:
    doy = dt_utc.timetuple().tm_yday
    year_dig = dt_utc.year - 2020
    return (
        f"{year_dig * 1000 + doy:04d}"
        f"{dt_utc.hour:02d}{dt_utc.minute:02d}{dt_utc.second:02d}"
        f"{ref3[:3].zfill(3)}"
    )


def _gen_sbp_id_semantic(dt_msk: datetime, bank: str, *, salt: bytes = b"") -> str:
    """32-char SBP: type | calendar/time | reference | control | channel | core | tail."""
    import hashlib
    import secrets

    lead, route_tail = _bank_route(bank, dt_msk)
    min_lag, max_lag = _sbp_lag_bounds(dt_msk)
    lag_sec = secrets.randbelow(max_lag - min_lag + 1) + min_lag
    dt_utc = _sbp_utc_clock(dt_msk, lag_sec)
    hm = hashlib.md5((salt or secrets.token_bytes(8)) + secrets.token_bytes(4)).digest()
    ref3 = f"{hm[0] * 10 + (hm[1] % 10):03d}"[-3:]
    # reference [11:17] = ref3 + two digits + terminal '0'.
    core3 = f"{hm[2] % 10}{hm[3] % 10}0"
    body = lead + _sbp_calendar_head(dt_utc, ref3) + core3 + route_tail
    return body[:32]


def _sbp_live_core_ok(raw: str) -> bool:
    s = (raw or "").replace(_NBSP, "").strip().upper()
    if len(s) != 32:
        return False
    return s[18:22] == _SBP_LIVE_ROUTE4 and s[22:27] == _SBP_LIVE_CORE and s[27:32] == _SBP_LIVE_BANK5


def _sbp_id_model_ok(raw: str, dt_msk: datetime, bank: str) -> bool:
    s = (raw or "").replace(_NBSP, "").strip()
    if not _sbp_id_structure_ok(s):
        return False
    lead, tail = _bank_route(bank, dt_msk)
    route_ok = bool(lead and tail and s[0] == lead and s.endswith(tail))
    if not route_ok and not _sbp_live_core_ok(s):
        return False
    try:
        cal = int(s[1:5])
        year = 2020 + cal // 1000
        doy = cal % 1000
        encoded_utc = datetime(year, 1, 1) + timedelta(days=doy - 1)
        encoded_utc = encoded_utc.replace(
            hour=int(s[5:7]), minute=int(s[7:9]), second=int(s[9:11]),
        )
    except (ValueError, OverflowError):
        return False
    encoded_local = encoded_utc + timedelta(hours=3)
    op = dt_msk.replace(microsecond=0)
    if encoded_local >= op:
        return False
    # date_formed is minute-only (origs: «19:56 мск»). Proton treats that as :00.
    # Encoded local must not sit later in the same minute (19:49:41 > 19:49:00).
    formed_floor = op.replace(second=0, microsecond=0)
    if encoded_local > formed_floor:
        return False
    lag = (op - encoded_local).total_seconds()
    min_lag, max_lag = _sbp_lag_bounds(dt_msk)
    if lag < min_lag or lag > max_lag:
        return False
    return True


def _gen_sbp_id_for_ctx(ctx: AlfaOrigContext, date_in: str) -> str:
    bank = ""
    try:
        bank = (ctx.extract_at(*SBP_COORDS["recipient_bank"]) or "") if ctx else ""
    except Exception:
        bank = ""
    return _gen_sbp_id_semantic(_parse_dt(date_in), bank)


def _parse_dt(date_in: str) -> datetime:
    import secrets

    raw = (date_in or "").replace("\xa0", " ").replace(",", " ").strip()
    if _is_auto_datetime(raw):
        return now_msk()
    body = raw.split("мск")[0].strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(body, fmt)
        except ValueError:
            continue
        if fmt != "%d.%m.%Y %H:%M:%S":
            dt = dt.replace(second=secrets.randbelow(60))
        return dt
    return now_msk()


# Genuine C16 7-digit tails by clock (minutes from midnight). Not a
# fixed 1.49–2.10M evening band — 10:48 live original is 569543.
# Do not retune from NEG 3a FAIL: op tail 604894 at 11:02 was already on-profile.
_OP_TAIL_KNOTS = (
    (2 * 60 + 22, 12_818),
    (10 * 60 + 48, 569_543),
    (11 * 60 + 54, 736_198),
    (12 * 60 + 42, 883_650),
    (13 * 60 + 58, 1_117_809),
    (14 * 60 + 43, 1_283_449),
    (15 * 60 + 51, 1_536_028),
    (18 * 60 + 45, 1_736_948),
    (19 * 60 + 53, 1_913_448),
    (22 * 60 + 3, 2_511_380),
)


def _op_tail_center(dt: datetime) -> int:
    """7-digit C16 tail from the hour-of-day corpus profile."""
    minutes = dt.hour * 60 + dt.minute
    knots = _OP_TAIL_KNOTS
    if minutes <= knots[0][0]:
        return knots[0][1]
    if minutes >= knots[-1][0]:
        return knots[-1][1]
    for (m0, t0), (m1, t1) in zip(knots, knots[1:]):
        if m0 <= minutes <= m1:
            if m1 == m0:
                return t0
            frac = (minutes - m0) / float(m1 - m0)
            return int(t0 + frac * (t1 - t0))
    return knots[-1][1]


def _gen_sbp_op_num(dt: datetime) -> str:
    """SBP: C16 + DDMMYY + 7 цифр (корпус).

    Формат ^C16\\d{13}$, дата [3:9]=DDMMYY.
    НЕ вшивать HHMMSS — иначе ALFA_KNOWN_GENERATOR_OPERATION_TIME_EMBEDDING.
    Хвост — серийник этого часа (10:48 → 569543), не вечерняя полоса 1.49–2.10 млн.
    """
    date_part = dt.strftime("%d%m%y")
    forbidden = dt.strftime("%H%M%S")
    used = _load_op_set()
    import secrets

    center = _op_tail_center(dt)
    # Clock correlation is strong but originals do not land on the exact
    # interpolation point. Use a non-zero local residual before collision scan.
    # 10.08 orig sits on the knot; 15.08 orig is ~66k below interpolation.
    residual = 2_500 + secrets.randbelow(70_000)
    anchor = center + (residual if secrets.randbits(1) else -residual)
    for i in range(0, 200_000):
        if i == 0:
            tail = anchor
        else:
            sign = 1 if i % 2 else -1
            tail = anchor + sign * ((i + 1) // 2)
        if tail < 1 or tail > 9_999_999:
            continue
        tail7 = f"{tail:07d}"
        if tail7[:6] == forbidden:
            continue
        cand = f"C16{date_part}{tail7}"
        if cand not in used:
            return cand
    tail7 = f"{anchor:07d}"
    return f"C16{date_part}{tail7}"


def _randomize_trailer_id(pdf: bytes) -> bytes:
    """Новый equal `/ID [<A><A>]` как у Oracle orig.

    Never silent-keep donor /ID (regex miss). Never unequal pair.
    Avoid corpus + already-sent IDs so validators do not see
    «номер документа не соответствует оригиналу» on a body clone.
    """
    pat = rb"/ID\s*\[\s*<[0-9A-Fa-f]{32}>\s*<[0-9A-Fa-f]{32}>\s*\]"
    if not re.search(pat, pdf):
        logger.warning("Alfa trailer /ID: pattern missing — leave unchanged")
        return pdf
    banned = _corpus_pdf_ids() | _sent_pdf_ids()
    for _ in range(48):
        rid_s = f"{random.getrandbits(128):032x}"
        if rid_s in banned:
            continue
        rid = rid_s.encode("ascii")
        out, n = re.subn(
            pat,
            b"/ID [<" + rid + b"><" + rid + b">]",
            pdf,
            count=1,
        )
        if n != 1:
            logger.warning("Alfa trailer /ID: replace failed n=%s", n)
            return pdf
        got = _pdf_trailer_ids(out)
        if got == (rid_s, rid_s):
            return out
    logger.warning("Alfa trailer /ID: exhausted unique equal-pair draws")
    return pdf


def _ensure_fresh_oracle_trailer_id(pdf: bytes) -> Optional[bytes]:
    """Randomize equal /ID; None if still corpus/sent/unequal/missing."""
    out = _randomize_trailer_id(pdf)
    if _trailer_id_reused(out):
        return None
    return out


_ALFA_PAYER_BIK = "044525593"  # счёт списания — БИК Альфы, не банк получателя
_TBANK_BIK = "044525974"
_ALFA_ACCT_HEAD = "40817810"  # 40817 + 810; 9th digit = key; then 11 digits
_ALFA_LIVE_LEDGERS = ("042", "056", "058", "059", "079", "082", "160")


def _ru_account_checksum(bik: str, account: str) -> int:
    """CBR 7-1-3 remainder. Valid account → 0 for that BIK."""
    digits = re.sub(r"\D", "", (bik or "")[-3:] + (account or ""))
    if len(digits) < 23:
        return -1
    weights = (7, 1, 3)
    return sum(int(digits[i]) * weights[i % 3] for i in range(23)) % 10


def _fix_account_checksum(account: str, bik: str = _ALFA_PAYER_BIK) -> str:
    """40817+810 + key + 11 digits. Key is the 9th digit (weight 3 vs BIK 593)."""
    acc = re.sub(r"\D", "", account or "")
    tail = (acc[-11:] if len(acc) >= 11 else acc).zfill(11)
    acc = _ALFA_ACCT_HEAD + "0" + tail
    if _ru_account_checksum(bik, acc) == 0:
        return acc
    base = _ru_account_checksum(bik, acc)
    for key in range(10):
        if (base + key * 3) % 10 == 0:
            return acc[:8] + str(key) + acc[9:]
    return acc


def _account_noise_ok(account: str) -> bool:
    """Reject ladder / run / period-10 tails that trip debit-account HARD."""
    digits = [int(ch) for ch in account if ch.isdigit()]
    if len(digits) != 20:
        return False
    asc = sum(1 for i in range(len(digits) - 1) if digits[i + 1] - digits[i] == 1)
    if asc >= 6:
        return False
    if re.search(r"(\d)\1{2,}", account):
        return False
    body = account[6:]
    if len(body) == 14 and all(body[i] == body[i + 10] for i in range(4)):
        return False
    return True


def _ledgers_for_route(tail: str) -> tuple:
    """Pin ledger to the SBP channel, not a random 15.08 Ozon 042 on B1013."""
    t = (tail or "").upper()
    if t.endswith("11821301"):
        return ("082",)
    if t.endswith("11830701"):
        return ("058", "059")
    if t.endswith("11760501"):
        return ("079",)
    if t.endswith("11770101"):
        return ("059",)
    return _ALFA_LIVE_LEDGERS


def _gen_alfa_debit_account(*, tail: str = "") -> str:
    """Alfa debit using a corpus ledger family, then checksum key + serial."""
    import secrets

    ledgers = _ledgers_for_route(tail)
    for _ in range(64):
        ledger = secrets.choice(ledgers)
        serial = f"{secrets.randbelow(10**8):08d}"
        acc_tail = ledger + serial
        acc = _fix_account_checksum(_ALFA_ACCT_HEAD + "0" + acc_tail)
        if acc[9:12] == ledger and _account_noise_ok(acc):
            return acc
    return _fix_account_checksum(_ALFA_ACCT_HEAD + "0" + "08270419628")


def _is_wb_bank(bank: str) -> bool:
    low = (bank or "").lower()
    return "wildberries" in low or "вайлдберриз" in low


def _wants_wb_chrome(bank: str) -> bool:
    """Long bank face on a 6-CID Т-Банк slot grows CS onto the 5155–5195 ridge."""
    b = (bank or "").replace("\xa0", " ").strip()
    if _is_wb_bank(b):
        return True
    # Сбербанк (8) is the longest name that stays clone-free on lean origs.
    return len(b) > 8


def _wants_wide_shell(prepared: Dict[str, str]) -> bool:
    """Long bank or long FIO: lean 5091 donors rewrite into the CS ridge."""
    recv = (prepared.get("receiver") or "").replace("\xa0", " ").strip()
    return _wants_wb_chrome(prepared.get("recipient_bank") or "") or len(recv) >= 20


def _onlypdf_size_ok(n: int, bank: str) -> tuple[bool, str]:
    """OnlyPDF local gate: lean ≤59087; Wildberries orig band ~59.8–60.7 KB."""
    if _ALFA_WB_SIZE_MIN <= n <= _ALFA_WB_SIZE_MAX and (
        _is_wb_bank(bank) or _wants_wb_chrome(bank)
    ):
        return True, "ok"
    if _is_wb_bank(bank):
        return False, f"size-wb:{n}"
    if 59_088 <= n < 60_400:
        return False, f"size-midgap:{n}"
    if n > _ALFA_LEAN_SIZE_MAX:
        return False, f"size-onlypdf:{n}"
    if n < _ALFA_SIZE_MIN:
        return False, f"size-low:{n}"
    return True, "ok"


def _cap_word(word: str) -> str:
    w = (word or "").strip()
    if not w:
        return "Иван"
    return w[0].upper() + w[1:].lower() if len(w) > 1 else w.upper()


def _synth_patronymic(first: str, *, female: bool) -> str:
    f = _cap_word(first)
    low = f.lower()
    if female:
        if low.endswith("ия"):
            return f[:-2] + "евна"
        if low.endswith("ья"):
            return f[:-2] + "евна"
        if low.endswith("а"):
            return f[:-1] + "овна"
        if low.endswith("я"):
            return f[:-1] + "евна"
        return f + "овна"
    if low.endswith("й"):
        return f[:-1] + "евич"
    if low.endswith("ь"):
        return f[:-1] + "евич"
    return f + "ович"


def _guess_female(first: str) -> bool:
    return (first or "")[-1:].lower() in "ая"


def _fmt_alfa_sbp_receiver(raw: str) -> str:
    """Oracle SBP: Имя Отчество И — third token is a single surname initial."""
    s = str(raw or "").strip()
    if not s:
        return "Алина Александровна А"
    parts = [p.rstrip(".") for p in re.findall(r"[А-ЯЁа-яёA-Za-z]+", s.replace(".", " "))]
    parts = [p for p in parts if p]
    if not parts:
        return "Алина Александровна А"
    if len(parts) >= 3:
        first = _cap_word(parts[0])
        mid = _cap_word(parts[1])
        if mid.lower().endswith(("ович", "евич", "овна", "евна", "ична", "инична")):
            initial = _cap_word(parts[2][:1])
            return f"{first} {mid} {initial}"
        initial = _cap_word(parts[2][:1])
        return f"{first} {mid} {initial}"
    first = _cap_word(parts[0])
    if len(parts) == 2 and len(parts[1]) == 1:
        female = _guess_female(first)
        patron = _synth_patronymic(first, female=female)
        initial = _cap_word(parts[1][:1])
        return f"{first} {patron} {initial}"
    surname = _cap_word(parts[1])
    female = _guess_female(first)
    patron = _synth_patronymic(first, female=female)
    initial = _cap_word(surname[:1])
    return f"{first} {patron} {initial}"


def _oracle_receiver_face(raw: str) -> str:
    """Oracle SBP: Имя Отчество И — third token is a single letter."""
    parts = _fmt_alfa_sbp_receiver(raw).split()
    return _NBSP.join(parts)


def _prepare_sbp(data: Dict, *, ctx: Optional[AlfaOrigContext] = None) -> Dict[str, str]:
    date_in = str(data.get("date_time") or data.get("date") or "сейчас")
    op_dt = _parse_dt(date_in)
    formed = _fmt_datetime(op_dt, with_seconds=False)
    bank = str(data.get("recipient_bank") or data.get("bank") or "Сбербанк").rstrip(_NBSP)
    hit = _match_corpus_bank(bank)
    if hit:
        bank = hit[0]
    sbp_raw = str(data.get("sbp_id") or data.get("spb_number") or "авто")
    if _is_auto_token(sbp_raw) or not _sbp_id_model_ok(sbp_raw, op_dt, bank):
        sbp_raw = _gen_sbp_id_semantic(
            op_dt, bank, salt=repr(sorted(data.items())).encode("utf-8", "replace"),
        )
    op = str(data.get("operation_num") or data.get("operation_number") or "")
    if _is_auto_token(op) or _op_used(op):
        op = _gen_sbp_op_num(op_dt)
    op = op.rstrip(_NBSP) + _NBSP
    _lead, route_tail = _bank_route(bank, op_dt)
    acct_raw = str(data.get("account", ""))
    if _is_auto_token(acct_raw):
        acct = _gen_alfa_debit_account(tail=route_tail)
    else:
        acct = _fix_account_checksum(acct_raw.rstrip(_NBSP))
        if not _account_noise_ok(acct):
            acct = _gen_alfa_debit_account(tail=route_tail)
    recv = _oracle_receiver_face(str(data.get("receiver", "")))
    phone = _fmt_phone(str(data.get("phone", ""))).rstrip(_NBSP)
    bank = _NBSP.join(bank.split())
    out = {
        "date_formed": formed.rstrip(_NBSP),
        "amount": _fmt_amount(str(data.get("amount", "0"))),
        "commission": _fmt_commission(),
        "date_time": _fmt_datetime(op_dt, with_seconds=True),
        "operation_num": op,
        "receiver": recv + _NBSP,
        "phone": phone,
        "recipient_bank": bank,
        "account": acct,
        "sbp_id": sbp_raw.replace(_NBSP, ""),
        "message": _ALFA_SBP_MESSAGE.rstrip(_NBSP),
    }
    return _fit_prepared_cs(out)


_SBP_CS_SKELETON = 4315  # Oracle SBP operators; 4 bytes per painted CID.


def _pred_sbp_cs(
    prep: Dict[str, str],
    *,
    extra_op: int = 0,
    phone: Optional[str] = None,
    amount: Optional[str] = None,
) -> int:
    amt = amount if amount is not None else (prep.get("amount") or "")
    if amt.endswith("RUR") and not amt.endswith(_NBSP):
        amt = amt + _NBSP
    comm = prep.get("commission") or ""
    if comm.endswith("RUR") and not comm.endswith(_NBSP):
        comm = comm + _NBSP
    op = (prep.get("operation_num") or "") + (_NBSP * extra_op)
    phone_s = phone if phone is not None else (prep.get("phone") or "")
    parts = [
        prep.get("date_formed") or "",
        amt,
        comm,
        prep.get("date_time") or "",
        op,
        prep.get("receiver") or "",
        phone_s,
        prep.get("recipient_bank") or "",
        prep.get("account") or "",
        prep.get("sbp_id") or "",
        prep.get("message") or "",
    ]
    return _SBP_CS_SKELETON + 4 * sum(len(p) for p in parts)


def _deacon_cs_penalty(bank: str, cs: int) -> int:
    """Empirical Deacon stability penalty for Sep 01/02 Alfa SBP."""
    low = re.sub(r"\s+", " ", (bank or "").replace(_NBSP, " ").lower().replace("ё", "е")).strip()
    is_akbars = ("ак барс" in low) or ("ak bars" in low)
    is_severny = ("северн" in low)
    is_ozon = ("озон" in low) or ("ozon" in low)
    if "сбер" in low:
        if cs in (5115, 5123):
            return 800
        if cs in (5091, 5095, 5099, 5103, 5107, 5127):
            return 0
    if "т-банк" in low or "тинькофф" in low:
        if cs == 5087:
            return 250
        if cs == 5115:
            return 450
        if cs == 5139:
            return 220
        if cs in (5099, 5103, 5107, 5139):
            return 0
    if "совком" in low:
        if cs in (5115, 5123):
            return 900
        if cs in (5127, 5143):
            return 0
    # 02.09 regressions from bot payloads:
    # Ак Барс / Северный were pinned to 5123/5127 and failed 24/24 probes.
    if is_akbars:
        if cs in (5123, 5127):
            return 900
        if cs in (5107, 5111, 5119, 5131):
            return 0
    if is_severny:
        if cs in (5123, 5127):
            return 900
        if cs in (5111, 5119, 5131, 5135):
            return 0
    # Ozon Sep payloads were stuck on 5143 and failed while the same face on
    # August date passed. Prefer nearby non-5143 bands first.
    if is_ozon:
        if cs == 5143:
            return 980
        if cs in (5127, 5131, 5135, 5139):
            return 0
    return 0


def _fit_prepared_cs(prep: Dict[str, str]) -> Dict[str, str]:
    """Walk phone/amount CID count so decoded CS is not a Deacon clone length.

    Face letters stay exact. Shell choice cannot change CS (constant skeleton).
    Op NBSP extras ≤2 are applied later in emit; this covers ridge faces
    where +2 still lands in 5155–5191.
    """
    from alfa_emit import _sbp_cs_need_bump

    def ok(p, extra=0, phone=None, amount=None) -> bool:
        return not _sbp_cs_need_bump(
            _pred_sbp_cs(p, extra_op=extra, phone=phone, amount=amount)
        )

    pred_base = _pred_sbp_cs(prep)
    if ok(prep) and _deacon_cs_penalty(prep.get("recipient_bank") or "", pred_base) == 0:
        return prep
    raw_phone = prep.get("phone") or ""
    raw_amt = (
        (prep.get("amount") or "")
        .replace(_NBSP, "")
        .replace("RUR", "")
        .replace(" ", "")
    )
    dt = _parse_dt(prep.get("date_time") or "")
    best = None
    best_score = 99
    # Orig phone + grouped thousands first. Compact / ungrouped only to leave
    # the 5155–5191 ridge. Extra op NBSP last — live Deacon FAKE (5087→5091).
    for grouped in (True, False):
        for compact in (0, 1, 2, 3):
            phone = _fmt_phone(raw_phone, compact=compact)
            amt = _fmt_amount(raw_amt, grouped=grouped)
            # Orig date_formed is always «HH:MM\xa0мск». Stripping the gap
            # walks CS by 4 (5111→5107) but is a visual tell — do not use it.
            formed = _fmt_datetime(dt, with_seconds=False, msk_gap=True)
            trial = dict(prep)
            trial["phone"] = phone
            trial["amount"] = amt
            trial["date_formed"] = formed
            extra_need = 0
            if ok(trial):
                extra_need = 0
            else:
                # Keep classic ridge first, but allow wider extra-op search for
                # bank/date clusters where Deacon rejects the default CS bucket.
                found = None
                for cand in (1,):
                    if ok(trial, cand):
                        found = cand
                        break
                if found is None:
                    continue
                extra_need = found
            if extra_need:
                trial["operation_num"] = cap_trailing_nbsp(
                    (trial.get("operation_num") or "") + (_NBSP * extra_need),
                    key="operation_num",
                )
            # Prefer orig grouped amount + extra op CID over ungrouped
            # «8140» (typography-bare) when leaving clone CS 5111.
            score = (
                compact * 40
                + (0 if grouped else 50)
                + extra_need * 8
            )
            pred = _pred_sbp_cs(trial)
            score += _deacon_cs_penalty(trial.get("recipient_bank") or "", pred)
            if score < best_score:
                best, best_score = trial, score
    if best is not None:
        bank_face = best.get("recipient_bank") or ""
        pred0 = _pred_sbp_cs(best)
        if _deacon_cs_penalty(bank_face, pred0):
            # Last CS nudge axis: trailing NBSP in message (face-safe).
            # This shifts decoded CS by +4 per NBSP without touching user fields.
            for bump in (1, 2):
                trial = dict(best)
                trial["message"] = cap_trailing_nbsp(
                    (trial.get("message") or "") + (_NBSP * bump), key="message",
                )
                pred = _pred_sbp_cs(trial)
                if _deacon_cs_penalty(bank_face, pred):
                    continue
                # Prefer moving to lower-risk CS even when old clone-len heuristic
                # marks it borderline; Deacon bank-specific risk is primary here.
                if _sbp_cs_need_bump(pred) and _deacon_cs_penalty(bank_face, pred) >= _deacon_cs_penalty(bank_face, pred0):
                    continue
                best = trial
                best_score += bump
                break
        if best_score:
            logger.info(
                "Alfa SBP cs-fit score=%d pred=%d phone=%r amt=%r formed=%r",
                best_score,
                _pred_sbp_cs(best),
                best.get("phone"),
                best.get("amount"),
                best.get("date_formed"),
            )
        return best
    return prep


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


_DT_FACE_RE = re.compile(
    r"\d{2}\.\d{2}\.\d{4}.+\d{2}:\d{2}:\d{2}.+мск",
)


def _oracle_sbp_shell_ok(path: str) -> bool:
    """Skip card/phone/broken origs whose SBP_COORDS do not paint a receipt.

    ``альфа сбпп.pdf`` labels ВТБ but date_time sits on the amount and
    receiver sits on the op number — Deacon FAKE (16.06 ВТБ, CS 5570).
    """
    ctx = AlfaOrigContext()
    if not ctx.load(path):
        return False
    dt = (ctx.extract_at(*SBP_COORDS["date_time"]) or "").replace(_NBSP, " ")
    recv = (ctx.extract_at(*SBP_COORDS["receiver"]) or "").replace(_NBSP, " ").strip()
    bank = (ctx.extract_at(*SBP_COORDS["recipient_bank"]) or "").replace(_NBSP, " ")
    amt = ctx.extract_at(*SBP_COORDS["amount"]) or ""
    if not _DT_FACE_RE.search(dt):
        return False
    if not recv or recv[:1].isdigit() or recv.upper().startswith("C16"):
        return False
    if "RUR" in dt or "RUR" in recv or "RUR" in bank:
        return False
    if "RUR" not in amt:
        return False
    return True


def _op_trailing_nbsp_ok(ctx: AlfaOrigContext) -> bool:
    """Orig op id ends with one NBSP. ≥3 trailing NBSP is G-SEM-NBSP-001 FAKE."""
    got = ctx.extract_at(*SBP_COORDS["operation_num"]) or ""
    n = 0
    for ch in reversed(got):
        if ch == _NBSP:
            n += 1
        else:
            break
    return 1 <= n <= 2


def _date_formed_ok(ctx: AlfaOrigContext) -> bool:
    """Orig formed line is «DD.MM.YYYY HH:MM\xa0мск» with no trailing NBSP."""
    got = ctx.extract_at(*SBP_COORDS["date_formed"]) or ""
    if not got or got.endswith(_NBSP):
        return False
    return "\xa0мск" in got


def _date_time_trailing_ok(ctx: AlfaOrigContext) -> bool:
    """Seconds line must keep the orig trailing NBSP after «мск»."""
    got = ctx.extract_at(*SBP_COORDS["date_time"]) or ""
    if not _DT_FACE_RE.search(got.replace(_NBSP, " ")):
        return False
    return got.endswith(_NBSP)


def _layout_shells() -> list:
    """Oracle page shells (images + operators). Font is emitted from scratch."""
    from alfa_corpus import canonical_paths

    lean, fat = [], []
    for p in canonical_paths("sbp") or []:
        if not p or not os.path.isfile(p):
            continue
        if "unlock" in os.path.basename(p).lower():
            continue
        if not _oracle_sbp_shell_ok(p):
            continue
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        (lean if 57_000 <= sz <= 61_000 else fat).append(p)
    # Prefer the live 10.08 Oracle shell, then other originals in-band.
    # 15.08 WB 60601 is an orig (~61 KB), not unlocked fat.
    def _shell_key(p: str):
        name = os.path.basename(p)
        return (name != _LIVE_ORACLE_SHELL, os.path.getsize(p))

    lean.sort(key=_shell_key)
    fat.sort(key=_shell_key)
    return lean + fat


def _slot_bank(text: str) -> str:
    return (text or "").replace(_NBSP, " ").strip()


def _shells_for_face(prepared: Dict[str, str]) -> list:
    """Prefer an orig whose bank/amount slots already match the face.

    ПСБ on the 10.08 Т-Банк shell nets decoded CS to donor 5111 → OnlyPDF
    FAKE. The corpus orig ``альфа сбп.pdf`` already paints ПСБ (3 CIDs).
    """
    pool = _layout_shells()
    want_bank = _slot_bank(prepared.get("recipient_bank"))
    want_amt = len(prepared.get("amount") or "")
    want_recv = len(prepared.get("receiver") or "")
    wb_bank = _is_wb_bank(want_bank)
    scored = []
    for path in pool:
        ctx = AlfaOrigContext()
        if not ctx.load(path) or not _oracle_sbp_shell_ok(path):
            continue
        bank = _slot_bank(ctx.extract_at(*SBP_COORDS["recipient_bank"]))
        amt = ctx.extract_at(*SBP_COORDS["amount"]) or ""
        recv = ctx.extract_at(*SBP_COORDS["receiver"]) or ""
        sz = os.path.getsize(path)
        long_fio = want_recv >= 20
        long_bank = _wants_wb_chrome(want_bank)
        wide = wb_bank or long_bank or long_fio
        if wide:
            size_band = 0 if sz >= 60_500 else 1
        else:
            size_band = 0 if (sz <= 58_500 or sz >= 60_500) else 1
        try:
            from alfa_emit import _SAFE_DONOR_CS, _sbp_cs_need_bump as _need_bump

            clone = 0 if len(ctx.stream) in _SAFE_DONOR_CS else 1
            if _need_bump(len(ctx.stream)):
                clone = 1
            cs_fit = abs(len(ctx.stream) - 5091)
        except Exception:
            clone = 0
            cs_fit = 99
        if wide:
            scored.append((
                size_band,
                clone,
                0 if bank == want_bank else 1,
                cs_fit,
                abs(len(amt) - want_amt),
                abs(len(recv) - want_recv),
                os.path.basename(path) != _LIVE_ORACLE_SHELL,
                path,
            ))
        else:
            scored.append((
                clone,
                cs_fit,
                0 if bank == want_bank else 1,
                size_band,
                abs(len(amt) - want_amt),
                abs(len(recv) - want_recv),
                os.path.basename(path) != _LIVE_ORACLE_SHELL,
                path,
            ))
    scored.sort()
    if scored and want_bank:
        logger.info(
            "Alfa SBP shell-face bank=%s first=%s",
            want_bank, os.path.basename(scored[0][-1]),
        )
    return [row[-1] for row in scored] or pool


def create_alfa_sbp_stealth(
    data: Dict,
    *,
    allow_repeat: bool = False,
    allow_ff2_repeat: bool = False,
    claim_minute: bool = True,
    shells: Optional[list] = None,
) -> Optional[bytes]:
    from alfa_emit import (
        _ORACLE_SBP_HMTX_UNIQ_FLOOR,
        emit_invariants,
        emit_onto_shell,
    )
    from alfa_font_extend import (
        _ff2_read_decompressed,
        _load_font_xrefs_from_bytes,
        _ot_checksum_matches,
    )
    from alfa_op_minutes import remember_prepared, stamp_unique_minute
    from alfa_oracle_master import ensure_master

    ensure_master()
    bank_in = str(data.get("recipient_bank") or data.get("bank") or "Сбербанк")
    if not _match_corpus_bank(bank_in):
        logger.error("Alfa SBP: empty recipient bank")
        return None
    blocked = _blocked_alfa_chars(data)
    if blocked:
        # Soft-warn only — never refuse UX; emit uses source-shared Tahoma parent.
        logger.warning(
            "Alfa SBP soft-ship rare chars: %s", "".join(dict.fromkeys(blocked)),
        )
        try:
            from alfa_oracle_master import ensure_parent_covers

            ensure_parent_covers("".join(dict.fromkeys(blocked)))
        except Exception as exc:
            logger.warning("Alfa SBP parent-cover: %s", exc)
    if not allow_repeat and _identity_blocked(_prepare_sbp(data)):
        # Soft-ship: same face retry must still emit (bot UX).
        logger.warning("Alfa SBP soft-ship duplicate identity")
    data = dict(data)
    if claim_minute:
        stamp_unique_minute(data, _parse_dt, channel="alfa_sbp")

    shells = list(shells) if shells else []
    prepared0 = _prepare_sbp(dict(data))
    if not shells:
        seen: set = set()
        shells = []
        for p in _iter_donors(data) + _shells_for_face(prepared0):
            if p and p not in seen:
                seen.add(p)
                shells.append(p)
    if not shells:
        logger.error("Alfa SBP: нет Oracle shell")
        return None

    def _shell_weight(path: str) -> tuple:
        """Bank match first — size-only sort painted ПСБ onto the 10.08 Т-Банк shell."""
        want_bank = _slot_bank(prepared0.get("recipient_bank"))
        want_recv = len(prepared0.get("receiver") or "")
        try:
            sz = os.path.getsize(path)
        except OSError:
            return (9, 99, 1, 99, 9, 999_999, path)
        bank_miss = 1
        recv_delta = 99
        clone = 1
        cs_fit = 99
        ctx = AlfaOrigContext()
        if ctx.load(path):
            bank_miss = (
                0
                if _slot_bank(ctx.extract_at(*SBP_COORDS["recipient_bank"])) == want_bank
                else 1
            )
            recv_delta = abs(len(ctx.extract_at(*SBP_COORDS["receiver"]) or "") - want_recv)
            cs_fit = 99
            try:
                from alfa_emit import _SAFE_DONOR_CS, _sbp_cs_need_bump as _need_bump

                clone = 0 if len(ctx.stream) in _SAFE_DONOR_CS else 1
                if _need_bump(len(ctx.stream)):
                    clone = 1
                cs_fit = abs(len(ctx.stream) - 5091)
            except Exception:
                clone = 0
        wb = _is_wb_bank(prepared0.get("recipient_bank") or "")
        wide = _wants_wb_chrome(prepared0.get("recipient_bank") or "")
        if wb or wide:
            size_band = 0 if sz >= 60_500 else 1
        elif sz <= 58_500:
            size_band = 0
        elif sz >= 60_500:
            size_band = 1
        else:
            size_band = 2
        return (clone, cs_fit, bank_miss, recv_delta, size_band, sz, path)

    shells.sort(key=_shell_weight)
    size0 = [p for p in shells if _shell_weight(p)[4] == 0]
    size1 = [p for p in shells if _shell_weight(p)[4] == 1]
    rest = [p for p in shells if p not in size0 and p not in size1]
    if _is_wb_bank(prepared0.get("recipient_bank") or "") or _wants_wb_chrome(
        prepared0.get("recipient_bank") or ""
    ):
        shells.sort(key=lambda p: (_shell_weight(p)[4], _shell_weight(p)[0], p))
        size0 = [p for p in shells if _shell_weight(p)[4] == 0]
        size1 = [p for p in shells if _shell_weight(p)[4] == 1]
        rest = [p for p in shells if p not in size0 and p not in size1]
        shell_cycle = (size0 * 4 or shells) + size1 + rest
    else:
        shell_cycle = (shells[:1] * 4 + shells[1:]) if shells else []

    def _ship_pdf(pdf: bytes, prepared: Dict[str, str], *, trial: int) -> bytes:
        sha = _ff2_sha16(pdf)
        prefixes = set(re.findall(rb"/([A-Z]{6})\+Tahoma", pdf))
        if len(prefixes) != 1:
            logger.warning(
                "Alfa SBP soft-ship prefix-count:%d trial=%d", len(prefixes), trial,
            )
            prefix = next(iter(prefixes)) if prefixes else b"SOFTUX"
        else:
            prefix = next(iter(prefixes))
        if prefix in _corpus_subset_tags() or prefix in _sent_prefix_map():
            logger.warning(
                "Alfa SBP soft-ship prefix-reused:%s trial=%d",
                prefix.decode("ascii", "replace"), trial,
            )
        chk_local = AlfaOrigContext()
        if not chk_local.load_bytes(pdf):
            return pdf
        cid_signature = _cid_map_signature(chk_local)
        if cid_signature in _sent_cid_signatures():
            logger.warning("Alfa SBP soft-ship cid-map-reused trial=%d", trial)
        pdf_sha = hashlib.sha256(pdf).hexdigest()[:16]
        if pdf_sha in _BURNED_PDF_SHA16:
            logger.warning(
                "Alfa SBP soft-ship burned-pdf-sha:%s trial=%d", pdf_sha, trial,
            )
        if sha in _BANNED_FF2_SHA16 or (
            not allow_ff2_repeat
            and (
                sha in _PASS_FF2_SHA16
                or (sha in _sent_ff2_shas() and sha not in _corpus_ff2_shas())
            )
        ):
            logger.warning("Alfa SBP soft-ship ff2-clone:%s trial=%d", sha, trial)
        if sha not in _corpus_ff2_shas():
            _remember_ff2_sha(sha)
        _remember_prefix(prefix, sha, cid_signature)
        _remember_sent_payload(
            prepared,
            pdf,
            prefix=prefix,
            ff2_sha=sha,
            cid_signature=cid_signature,
        )
        _remember_prepared_identity(prepared)
        remember_prepared(prepared, channel="alfa_sbp")
        logger.info("🔴 ALFA SBP EMIT: %d bytes trial=%d ff2=%s", len(pdf), trial, sha)
        return pdf

    best_any: Optional[bytes] = None
    last_why = ""
    for trial in range(24):
        work = dict(data)
        if trial and (
            _is_auto_token(data.get("operation_num") or data.get("operation_number"))
            or _op_used(str(data.get("operation_num") or ""))
        ):
            work["operation_num"] = "авто"
        if trial and _is_auto_token(data.get("sbp_id") or data.get("spb_number")):
            work["sbp_id"] = "авто"
        prepared = _prepare_sbp(work)
        op_dt = _parse_dt(str(work.get("date_time") or work.get("date") or "сейчас"))
        if not _sbp_id_model_ok(prepared["sbp_id"], op_dt, prepared["recipient_bank"]):
            prepared["sbp_id"] = _gen_sbp_id_semantic(
                op_dt, prepared["recipient_bank"], salt=bytes([trial]),
            )
            last_why = "SBP mismatch"
            if not _sbp_id_model_ok(prepared["sbp_id"], op_dt, prepared["recipient_bank"]):
                logger.info("Alfa SBP rebuild SBP mismatch trial=%d", trial)
                continue
        reused_field = _payload_reuse_field(prepared)
        if reused_field:
            # Soft-ship: user-fixed face fields collide after first successful send.
            logger.warning(
                "Alfa SBP soft-ship reused-%s trial=%d", reused_field, trial,
            )
        seed = hashlib.sha256(
            repr(sorted(prepared.items())).encode("utf-8") + bytes([trial])
        ).digest()
        path = shell_cycle[trial % len(shell_cycle)]
        try:
            with open(path, "rb") as fh:
                shell = fh.read()
        except OSError:
            last_why = "xref/Length mismatch shell-read"
            continue
        try:
            # SBP: never match_cs=True — that pads short FIO with extra NBSP
            # (Deacon FAKE). Clone CS lengths are walked via message, not FIO.
            pdf, why = emit_onto_shell(
                shell, prepared, SBP_COORDS, seed, profile="oracle",
                match_cs=False,
                hmtx_uniq_floor=_ORACLE_SBP_HMTX_UNIQ_FLOOR,
            )
        except Exception as exc:
            last_why = f"xref/Length mismatch {type(exc).__name__}"
            logger.info("Alfa SBP rebuild %s trial=%d shell=%s", last_why, trial, os.path.basename(path))
            continue
        if pdf is None:
            last_why = why or "emit"
            if last_why == "text overflow":
                fallback_pdf = _attempt(path, work, tag=f"FALLBACK{trial}")
                if fallback_pdf:
                    fb = AlfaOrigContext()
                    if (
                        fb.load_bytes(fallback_pdf)
                        and _rur_trailing_nbsp_ok(fb)
                        and _fio_trailing_nbsp_ok(fb)
                    ):
                        return fallback_pdf
            logger.info("Alfa SBP rebuild %s trial=%d shell=%s", last_why, trial, os.path.basename(path))
            continue
        if pdf != shell:
            pdf = _ensure_fresh_oracle_trailer_id(pdf)
            if pdf is None:
                last_why = "identity mismatch reused-pdf-id"
                continue
        chk = AlfaOrigContext()
        if not chk.load_bytes(pdf):
            last_why = "xref/Length mismatch verify"
            continue
        # Prefer a fresh internal identity before shipping:
        # repeated FF2/CID clusters are accepted by local invariants but
        # empirically unstable on live Deacon checks for Sep 01/02 Alfa SBP.
        quality_risks = []
        stream_cs = len(chk.stream or b"")
        if _deacon_cs_penalty(prepared.get("recipient_bank") or "", stream_cs) >= 450:
            quality_risks.append(f"cs-risk:{stream_cs}")
        ff2_now = _ff2_sha16(pdf)
        if ff2_now in _sent_ff2_shas() and ff2_now not in _corpus_ff2_shas():
            quality_risks.append(f"ff2-repeat:{ff2_now}")
        cid_now = _cid_map_signature(chk)
        if cid_now in _sent_cid_signatures():
            quality_risks.append("cid-repeat")
        if quality_risks and trial < 18:
            last_why = "quality/" + ",".join(quality_risks)
            logger.info("Alfa SBP rebuild %s trial=%d shell=%s", last_why, trial, os.path.basename(path))
            continue
        if not _verify_committed(pdf, prepared):
            last_why = "text overflow"
            continue
        if not _date_time_trailing_ok(chk):
            last_why = "glyph mismatch date-time"
            logger.info("Alfa SBP rebuild date-time slot trial=%d shell=%s", trial, os.path.basename(path))
            continue
        if not _date_formed_ok(chk):
            last_why = "glyph mismatch date-formed"
            logger.info("Alfa SBP rebuild date-formed pad trial=%d shell=%s", trial, os.path.basename(path))
            continue
        if not _op_trailing_nbsp_ok(chk):
            last_why = "glyph mismatch op-nbsp"
            logger.info("Alfa SBP rebuild op-nbsp trial=%d shell=%s", trial, os.path.basename(path))
            continue
        if not _rur_trailing_nbsp_ok(chk) or not _fio_trailing_nbsp_ok(chk):
            last_why = "glyph mismatch trailing-nbsp"
            fallback_pdf = _attempt(path, work, tag=f"FALLBACK{trial}")
            if fallback_pdf:
                fb = AlfaOrigContext()
                if (
                    fb.load_bytes(fallback_pdf)
                    and _rur_trailing_nbsp_ok(fb)
                    and _fio_trailing_nbsp_ok(fb)
                    and _date_time_trailing_ok(fb)
                    and _date_formed_ok(fb)
                    and _op_trailing_nbsp_ok(fb)
                ):
                    pdf = fallback_pdf
                    chk = fb
                else:
                    logger.info("Alfa SBP rebuild fallback trailing-nbsp trial=%d", trial)
                    continue
            else:
                logger.info("Alfa SBP rebuild trailing-nbsp trial=%d", trial)
                continue
        sbp_got = chk.extract_at(*SBP_COORDS["sbp_id"])
        if not _sbp_id_structure_ok(sbp_got) or not _sbp_id_model_ok(
            sbp_got, op_dt, prepared["recipient_bank"],
        ):
            last_why = "SBP mismatch verify"
            logger.info("Alfa SBP rebuild %s trial=%d", last_why, trial)
            continue
        refs = _load_font_xrefs_from_bytes(pdf)
        landed = _ff2_read_decompressed(pdf, refs["ff2"]) if refs else b""
        if not landed or not _ot_checksum_matches(landed):
            logger.warning("Alfa SBP soft-ship ot-checksum trial=%d", trial)
        if not _fontfile2_has_exact_sfnt_end(pdf):
            logger.warning("Alfa SBP soft-ship sfnt-tail trial=%d", trial)
        why = emit_invariants(pdf, channel="sbp")
        if why:
            if why.startswith("cs-len:") or why.startswith("glyph mismatch"):
                last_why = why
                if why.startswith("cs-len:"):
                    if best_any is None or len(pdf) < len(best_any):
                        best_any = pdf
                logger.info("Alfa SBP rebuild %s trial=%d shell=%s", why, trial, os.path.basename(path))
                continue
            logger.warning("Alfa SBP soft-ship invariants %s trial=%d", why, trial)
        # Fast-path emit_onto_shell may leave unused printable CIDs in the font
        # subset (Proton HARD: ALFA_ORACLE_FONT_SUBSET_CLOSURE_VIOLATION).
        # Neutralize ToUnicode for a small set of orphans before any hashes/signatures.
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
                        logger.warning("Alfa SBP soft-ship closure-fix reload")
                    else:
                        why = emit_invariants(pdf, channel="sbp")
                        if why:
                            logger.warning("Alfa SBP soft-ship after closure: %s", why)
        except Exception as exc:
            logger.warning("Alfa SBP closure-fix fast-path skip: %s", exc)
        # Lean origs top out at 59087 (15.08 Ozon late). WB orig is 60601.
        bank_face = prepared.get("recipient_bank") or ""
        ok_sz, sz_why = _onlypdf_size_ok(len(pdf), bank_face)
        if ok_sz:
            return _ship_pdf(pdf, prepared, trial=trial)
        if _ALFA_WB_SIZE_MIN <= len(pdf) <= _ALFA_WB_SIZE_MAX:
            logger.warning(
                "Alfa SBP soft-ship wb-band size:%d bank=%s trial=%d",
                len(pdf), bank_face, trial,
            )
            return _ship_pdf(pdf, prepared, trial=trial)
        if sz_why.startswith("size-midgap") or sz_why.startswith("size-onlypdf"):
            last_why = sz_why
            if best_any is None or len(pdf) < len(best_any):
                best_any = pdf
            logger.info(
                "Alfa SBP rebuild %s:%d trial=%d shell=%s",
                sz_why, len(pdf), trial, os.path.basename(path),
            )
            continue
        if sz_why.startswith("size-wb"):
            last_why = sz_why
            logger.info(
                "Alfa SBP rebuild %s trial=%d shell=%s",
                sz_why, trial, os.path.basename(path),
            )
            continue

        # Size: never block ship — soft-log only (no HARD weight gate).
        if not (_ALFA_SIZE_MIN <= len(pdf) <= _ALFA_SIZE_MAX):
            logger.warning("Alfa SBP soft-ship size:%d trial=%d", len(pdf), trial)
        return _ship_pdf(pdf, prepared, trial=trial)

    # Last resort: exact-flate on lean donor shells first.
    for path in (shell_cycle[:8] or shells[:8]):
        pdf = _attempt(path, data, tag="FINAL")
        if not pdf:
            continue
        prep = _prepare_sbp(data)
        ok_sz, _ = _onlypdf_size_ok(len(pdf), prep.get("recipient_bank") or "")
        if ok_sz:
            logger.info(
                "🔴 ALFA SBP EMIT (FINAL lean): %d bytes shell=%s",
                len(pdf), os.path.basename(path),
            )
            return _ship_pdf(pdf, prep, trial=100)
        if best_any is None or len(pdf) < len(best_any):
            best_any = pdf

    if best_any:
        logger.warning(
            "Alfa SBP soft-ship mid-gap fallback:%d (%s)",
            len(best_any), last_why,
        )
        return _ship_pdf(best_any, _prepare_sbp(data), trial=101)

    logger.error("Alfa SBP: все пути не удались (%s)", last_why)
    return None


def check_text(text: str) -> list:
    from alfa_oracle_master import missing_chars

    found = missing_chars(text)
    for ch in text or "":
        if ch in _BLOCKED_FACE_LETTERS and ch not in found:
            found.append(ch)
    return found
