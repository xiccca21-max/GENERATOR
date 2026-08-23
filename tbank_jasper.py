# -*- coding: utf-8 -*-
"""Native T-Bank emit: JasperReports 6.20.3 / OpenPDF 1.3.30 (no donor PDF)."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
_JASPER_DIR = os.path.join(_DIR, "tbank-jasper")
_JAR_CANDIDATES = (
    os.path.join(_JASPER_DIR, "tbank-receipt.jar"),
    os.path.join(_JASPER_DIR, "target", "tbank-receipt.jar"),
)
_FONT_REG = os.path.join(_JASPER_DIR, "src", "main", "resources", "fonts", "TinkoffSans-Regular.ttf")
_FONT_MED = os.path.join(_JASPER_DIR, "src", "main", "resources", "fonts", "TinkoffSans-Medium.ttf")

_F1_DEC_LO, _F1_DEC_HI = 16244, 18196
_F1_V3_LO, _F1_V3_HI = 17121, 17198
_F2_SBP_LO, _F2_SBP_HI = 5000, 5824
_F2_GLYF_HI = 1549  # Fraudex fat / PASS recipe < 1550
_ORIG_SBP = os.path.join(_DIR, "templates", "T_sbp_original.pdf")
_HINT_TABLE_CACHE: Dict[str, dict] = {}


def jar_path() -> Optional[str]:
    for path in _JAR_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def java_bin() -> str:
    return os.environ.get("JAVA_HOME") and os.path.join(
        os.environ["JAVA_HOME"], "bin", "java.exe" if os.name == "nt" else "java"
    ) or "java"


def _params(channel: str, prepared: Dict) -> Dict[str, str]:
    amount = str(prepared.get("new_amount") or "").replace("₽", "").replace("i", "")
    if amount and not amount.endswith(" "):
        amount += " "
    amount_big = str(prepared.get("new_amount_total") or amount)
    if amount_big and not amount_big.endswith(" "):
        amount_big += " "
    receipt = str(prepared.get("receipt_raw") or "")
    if receipt and not receipt.startswith("Квитанция"):
        receipt = f"Квитанция  \u2116 {receipt}"
    transfer = {
        "sbp": "По номеру телефона",
        "phone": "По номеру телефона",
        "card_tbank": "Клиенту Т-Банка",
        "card": "Клиенту Т-Банка",
        "nocomm": "На карту",
    }.get(channel, "По номеру телефона")
    comm = str(prepared.get("commission") or "0 ")
    if "Без" in comm:
        comm = "0 "
    if comm and not comm.endswith(" ") and channel == "sbp":
        comm += " "
    keywords = _keywords_for_date(str(prepared.get("new_date") or ""))
    out = {
        "date": str(prepared.get("new_date") or ""),
        "amount": amount,
        "amountBig": amount_big,
        "sender": str(prepared.get("sender") or ""),
        "receiver": str(prepared.get("receiver") or ""),
        "phone": str(prepared.get("phone") or ""),
        "bank": str(prepared.get("bank") or prepared.get("recipient_bank") or ""),
        "account": str(prepared.get("account") or ""),
        "card": str(prepared.get("card") or ""),
        "sbpId": str(prepared.get("sbp_id_raw") or prepared.get("sbp_id") or ""),
        "sbpSuffix": str(prepared.get("sbp_suffix_raw") or prepared.get("sbp_suffix") or ""),
        "receipt": receipt,
        "transfer": transfer,
        "status": "Успешно",
        "commission": comm,
        "keywords": keywords,
    }
    return out


def _keywords_for_date(date_time: str) -> str:
    import hashlib
    import re
    from datetime import datetime as _dt, timedelta as _td
    from tbank_sbp_stealth import _keywords_third_token

    receipt_dt = None
    clean = re.sub(r"\s+", " ", (date_time or "").strip())
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            receipt_dt = _dt.strptime(clean, fmt)
            break
        except ValueError:
            pass
    if receipt_dt is None:
        return ""
    formed = receipt_dt + _td(seconds=45)
    return (
        f"{formed.strftime('%d.%m.%Y %H:%M:%S')} | "
        f"{hashlib.md5(os.urandom(16)).hexdigest()} | "
        f"{_keywords_third_token(receipt_dt)}"
    )


def _glyf_table_length(ttf: bytes) -> int:
    if not ttf or len(ttf) < 12:
        return 0
    try:
        nt = int.from_bytes(ttf[4:6], "big")
        for i in range(min(nt, 64)):
            o = 12 + i * 16
            if ttf[o:o + 4] == b"glyf":
                return int.from_bytes(ttf[o + 12:o + 16], "big")
    except Exception:
        pass
    return 0


def _hint_tables_from_orig(kind: str) -> dict:
    """cvt/fpgm/prep from the bank SBP original — resource TTFs are unhinted."""
    if kind in _HINT_TABLE_CACHE:
        return _HINT_TABLE_CACHE[kind]
    out: dict = {}
    if not os.path.isfile(_ORIG_SBP):
        _HINT_TABLE_CACHE[kind] = out
        return out
    try:
        import fitz
        import tbank_unlock_template as tut
        from fontTools.ttLib import TTFont
        from io import BytesIO

        needle = "Medium" if kind == "Medium" else "Regular"
        doc = fitz.open(_ORIG_SBP)
        try:
            fm = tut._find_font_objects(doc)
            for name, info in fm.items():
                if needle not in name:
                    continue
                ff = doc.xref_stream(info["fontfile_xref"])
                ft = TTFont(BytesIO(ff))
                for tag in ("cvt ", "fpgm", "prep"):
                    if tag in ft:
                        out[tag] = ft[tag]
                break
        finally:
            doc.close()
    except Exception as exc:
        logger.warning("Jasper hint tables from orig: %s", exc)
    _HINT_TABLE_CACHE[kind] = out
    return out


def _inject_hint_tables(ff2: bytes, master_path: str) -> bytes:
    """OpenPDF drops cvt/fpgm/prep; bank FontFile2 keeps the full-font hint tables."""
    from io import BytesIO
    from fontTools.ttLib import TTFont

    sub = TTFont(BytesIO(ff2))
    changed = False
    if master_path and os.path.isfile(master_path):
        master = TTFont(master_path)
        for tag in ("cvt ", "fpgm", "prep"):
            if tag in master and tag not in sub:
                sub[tag] = master[tag]
                changed = True
    kind = "Medium" if "Medium" in os.path.basename(master_path or "") else "Regular"
    extra = _hint_tables_from_orig(kind)
    from copy import deepcopy
    for tag, table in extra.items():
        if tag not in sub:
            sub[tag] = deepcopy(table)
            changed = True
    if not changed:
        return ff2
    bio = BytesIO()
    sub.save(bio, reorderTables=False)
    return bio.getvalue()


def _pad_decoded_tail(ff2: bytes, lo: int, hi: int, banned: set) -> bytes:
    """Grow Length1 with a tail after SFNT tables — does not touch glyf/loca."""
    n = len(ff2)
    if lo <= n <= hi and n not in banned:
        return ff2
    if n >= lo:
        return ff2
    for extra in range(lo - n, hi - n + 1):
        landed = n + extra
        if landed in banned:
            continue
        if lo <= landed <= hi:
            out = ff2 + os.urandom(extra)
            logger.info("Jasper FF2 tail %d→%d", n, len(out))
            return out
    return ff2


_F1_DEC_BANNED = frozenset({17180, 17164, 16948})
_F2_DEC_BANNED = frozenset({5444, 5284, 5156, 5144})


def _pad_used_glyph_programs(
    ff2: bytes,
    keep_gids: set,
    lo: int,
    hi: int,
    banned: set,
) -> bytes:
    """Grow FontFile2 into [lo, hi] by entropy on used simple glyph hints.

    Native Jasper subset is not a corpus twin — mosaic hash does not apply.
    Avoid exact specimen decoded lengths (SIZE_MULTISET / mutated-twin).
    Bounded binary search; never the hanging V3 retarget loop.
    """
    from copy import deepcopy
    from io import BytesIO
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables.ttProgram import Program

    if lo <= len(ff2) <= hi and len(ff2) not in banned:
        return ff2

    def _names(data: bytes):
        ft = TTFont(BytesIO(data))
        glyf = ft["glyf"]
        go = ft.getGlyphOrder()
        keep = set(keep_gids) | {0, 3}
        names = []
        for gid, name in enumerate(go):
            if gid not in keep:
                continue
            g = glyf[name]
            if int(getattr(g, "numberOfContours", 0) or 0) > 0:
                names.append(name)
        return names

    names = _names(ff2)
    if not names:
        return ff2

    def _apply(total: int) -> bytes:
        ft = TTFont(BytesIO(ff2))
        glyf = ft["glyf"]
        n = len(names)
        base = total // n
        rem = total - base * n
        for i, name in enumerate(names):
            extra = base + (rem if i == 0 else 0)
            if extra <= 0:
                continue
            g = glyf[name]
            try:
                g.expand(glyf)
            except Exception:
                continue
            old = b""
            try:
                if getattr(g, "program", None) is not None:
                    old = bytes(g.program.getBytecode() or b"")
            except Exception:
                old = b""
            prog = Program()
            prog.fromBytecode(old + os.urandom(extra))
            g2 = deepcopy(g)
            g2.program = prog
            glyf[name] = g2
        bio = BytesIO()
        ft.save(bio, reorderTables=False)
        return bio.getvalue()

    need = max(32, lo - len(ff2) + 64)
    low_p, high_p = need, max(need + 256, (hi - len(ff2)) * 2 + 512)
    best = ff2
    for _ in range(16):
        if low_p > high_p:
            break
        mid = (low_p + high_p) // 2
        trial = _apply(mid)
        n = len(trial)
        if lo <= n <= hi and n not in banned:
            logger.info("Jasper FF2 pad %d→%d (hint %d)", len(ff2), n, mid)
            return trial
        if n < lo or n in banned:
            low_p = mid + 1
            if n > len(best) and n not in banned:
                best = trial
        else:
            high_p = mid - 1
    if lo <= len(best) <= hi and len(best) not in banned:
        return best
    logger.warning(
        "Jasper FF2 pad miss %d want %d..%d", len(best), lo, hi,
    )
    return best


def _hydrate_used_from_master(ff2: bytes, keep_gids: set, master_path: str) -> bytes:
    """Replace subset glyf/hmtx for painted GIDs with full-master outlines."""
    if not os.path.isfile(master_path):
        return ff2
    from io import BytesIO
    from fontTools.ttLib import TTFont

    sub = TTFont(BytesIO(ff2))
    master = TTFont(master_path)
    go_s = sub.getGlyphOrder()
    go_m = master.getGlyphOrder()
    changed = False
    for gid in sorted(set(keep_gids) | {0, 3}):
        if gid >= len(go_s) or gid >= len(go_m):
            continue
        ns, nm = go_s[gid], go_m[gid]
        try:
            sub["glyf"][ns] = master["glyf"][nm]
            if "hmtx" in sub and "hmtx" in master:
                sub["hmtx"].metrics[ns] = master["hmtx"].metrics[nm]
            changed = True
        except Exception:
            continue
    if not changed:
        return ff2
    bio = BytesIO()
    sub.save(bio, reorderTables=False)
    return bio.getvalue()


def _copy_master_unused_until(
    ff2: bytes, keep_gids: set, master_path: str, lo: int, hi: int, banned: set,
    glyf_hi: Optional[int] = None,
) -> bytes:
    """Fill unused GID slots with real master outlines until decoded lands in-band.

    Compresses like bank glyf (not urandom). Extra glyphs are not in CMap —
    native Jasper CMap only lists painted letters, same as OpenPDF.
    """
    if not os.path.isfile(master_path):
        return ff2
    from io import BytesIO
    from fontTools.ttLib import TTFont

    keep = set(keep_gids) | {0, 3}
    cur = ff2
    if lo <= len(cur) <= hi and len(cur) not in banned:
        return cur
    master = TTFont(master_path)
    go_m = master.getGlyphOrder()
    candidates = []
    for gid, name in enumerate(go_m):
        if gid in keep:
            continue
        g = master["glyf"][name]
        nc = int(getattr(g, "numberOfContours", 0) or 0)
        if nc == 0:
            continue
        candidates.append(gid)
    sized = []
    for gid in candidates:
        name = go_m[gid]
        g = master["glyf"][name]
        try:
            raw = g.compile(master["glyf"])
            sized.append((len(raw), gid))
        except Exception:
            sized.append((50, gid))
    sized.sort()

    sub = TTFont(BytesIO(cur))
    go_s = sub.getGlyphOrder()
    added = 0
    for _sz, gid in sized:
        if gid >= len(go_s):
            continue
        ns, nm = go_s[gid], go_m[gid]
        try:
            sub["glyf"][ns] = master["glyf"][nm]
            if "hmtx" in sub and "hmtx" in master:
                sub["hmtx"].metrics[ns] = master["hmtx"].metrics[nm]
        except Exception:
            continue
        added += 1
        if added % 4 == 0 or _sz > 200:
            bio = BytesIO()
            sub.save(bio, reorderTables=False)
            trial = bio.getvalue()
            n = len(trial)
            g = _glyf_table_length(trial)
            if glyf_hi is not None and g > glyf_hi:
                break
            if lo <= n <= hi and n not in banned:
                logger.info(
                    "Jasper FF2 master-fill %d→%d unused=%d glyf=%d",
                    len(ff2), n, added, g,
                )
                return trial
            if n > hi:
                break
            cur = trial
    if lo <= len(cur) <= hi and len(cur) not in banned:
        return cur
    return cur


def _grow_ff2(
    ff2: bytes, lo: int, hi: int, keep_gids: set, master_path: str, *,
    banned=(), hydrate: bool = True, hints: bool = False,
    glyf_hi: Optional[int] = None,
) -> bytes:
    out = _inject_hint_tables(ff2, master_path) if hints else ff2
    if hydrate:
        out = _hydrate_used_from_master(out, keep_gids, master_path)
    if lo <= len(out) <= hi and len(out) not in banned:
        if glyf_hi is None or _glyf_table_length(out) <= glyf_hi:
            return out
    if len(out) < lo:
        out = _copy_master_unused_until(
            out, keep_gids, master_path, lo, hi, set(banned),
            glyf_hi=glyf_hi,
        )
    if lo <= len(out) <= hi and len(out) not in banned:
        if glyf_hi is None or _glyf_table_length(out) <= glyf_hi:
            return out
    if len(out) > hi:
        logger.warning("Jasper FF2 overshoot %d > %d — keep pre-grow %d", len(out), hi, len(ff2))
        if lo <= len(ff2) <= hi:
            return ff2
        return ff2
    if glyf_hi is not None:
        return _pad_decoded_tail(out, lo, hi, set(banned))
    return _pad_used_glyph_programs(out, keep_gids, lo, hi, set(banned))


def _pin_fonts(pdf: bytes, channel: str) -> bytes:
    import fitz
    import tbank_unlock_template as tut
    from tbank_dynamic import _finalize_tbank_f1_epoch, _finalize_tbank_f2_epoch
    from tbank_sbp_stealth import (
        _gids_per_font_in_stream,
        _patch_fontfile2_xref,
    )

    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        fm = tut._find_font_objects(doc)
        cs = doc.xref_stream(doc[0].get_contents()[0])
        reg_gids, med_gids = _gids_per_font_in_stream(cs)
        fr = fm.get("TinkoffSans-Regular")
        fm2 = fm.get("TinkoffSans-Medium")
        ff1 = doc.xref_stream(fr["fontfile_xref"]) if fr else None
        ff2 = doc.xref_stream(fm2["fontfile_xref"]) if fm2 else None
        x1 = int(fr["fontfile_xref"]) if fr else None
        x2 = int(fm2["fontfile_xref"]) if fm2 else None
    finally:
        doc.close()

    if ff1 is not None and x1 is not None:
        tgt_lo, tgt_hi = _F1_V3_LO, _F1_V3_HI
        if not (tgt_lo <= len(ff1) <= tgt_hi):
            if _F1_DEC_LO <= len(ff1) <= _F1_DEC_HI:
                tgt_lo, tgt_hi = _F1_DEC_LO, _F1_DEC_HI
        new1 = _grow_ff2(
            ff1, tgt_lo, tgt_hi, set(reg_gids) | {0, 3}, _FONT_REG,
            banned=_F1_DEC_BANNED, hydrate=True,
        )
        if new1 != ff1:
            patched = _patch_fontfile2_xref(pdf, x1, new1)
            if patched:
                pdf = patched
    if ff2 is not None and x2 is not None and channel == "sbp":
        new2 = _grow_ff2(
            ff2, _F2_SBP_LO, _F2_SBP_HI, set(med_gids) | {0, 3}, _FONT_MED,
            banned=_F2_DEC_BANNED, hydrate=False, hints=True,
            glyf_hi=_F2_GLYF_HI,
        )
        if new2 != ff2:
            patched = _patch_fontfile2_xref(pdf, x2, new2)
            if patched:
                pdf = patched
    pdf = _finalize_tbank_f1_epoch(pdf)
    pdf = _finalize_tbank_f2_epoch(pdf)
    return pdf


def try_render(channel: str, prepared: Dict) -> Optional[bytes]:
    """Jasper native PDF, or None if Java/jar missing."""
    jar = jar_path()
    if not jar:
        logger.warning("T-Bank Jasper: jar missing")
        return None
    params = _params(channel, prepared)
    payload = json.dumps(params, ensure_ascii=False).encode("utf-8")
    try:
        proc = subprocess.run(
            [java_bin(), "-jar", jar, "--channel", channel],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("T-Bank Jasper: java not found")
        return None
    except Exception as exc:
        logger.warning("T-Bank Jasper: %s", exc)
        return None
    if proc.returncode != 0 or not proc.stdout.startswith(b"%PDF"):
        err = (proc.stderr or b"")[-800:].decode("utf-8", "replace")
        logger.warning("T-Bank Jasper fail rc=%s %s", proc.returncode, err)
        return None
    pdf = proc.stdout
    try:
        from tbank_sbp_stealth import _patch_pdf_metadata, _randomize_pdf_fingerprints

        pdf = _patch_pdf_metadata(pdf, str(prepared.get("new_date") or ""))
        pdf = _pin_fonts(pdf, channel)
        pdf = _randomize_pdf_fingerprints(pdf)
    except Exception as exc:
        logger.warning("T-Bank Jasper post: %s", exc)
    logger.info("T-Bank Jasper native %s %d bytes", channel, len(pdf))
    return pdf
