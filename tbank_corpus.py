"""
Корпус оригиналов T-Bank — классификация подметодов (без СБП).

Используется card / phone / nocomm / card_tbank генераторами для
выбора доноров и целевого веса PDF.
"""
from __future__ import annotations

import os
import re
import secrets
import statistics
from typing import Dict, List, Optional, Tuple

CORPUS_DIR_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "tbank_sbp_corpus"),
    os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "т банк"),
]


def _resolve_corpus_dir() -> str:
    for path in CORPUS_DIR_CANDIDATES:
        if os.path.isdir(path) and any(
            n.lower().endswith(".pdf") for n in os.listdir(path)
        ):
            return path
    return CORPUS_DIR_CANDIDATES[-1]


CORPUS_DIR = _resolve_corpus_dir()

_RECEIPT_NUM_RE = re.compile(r"1-\d{3}-\d{3}-\d{3}-\d{3}")
_RECEIPT_STRICT_RE = re.compile(r"^1-\d{3}-\d{3}-\d{3}-\d{3}$")
_SBP_ID_RE = re.compile(r"\b[AB][0-9A-Z]{26}\b")

# Все авто-номера Т-Банка: фиксированный блок 1-132-, дальше три группы рандом.
TBANK_RECEIPT_A = "132"
TBANK_RECEIPT_FALLBACK = "1-132-107-043-958"

_KIND_LABELS = {
    "nocomm": "На карту (без к-и / другой банк)",
    "card_tbank": "Клиенту Т-Банка",
    "card_sber": "По номеру карты",
    "phone": "По номеру телефона",
    "sbp": "СБП",
    "statement": "Выписка",
    "unknown": "неизвестно",
}

# Медианный вес оригиналов по подметоду (байт)
_SIZE_TARGET: Dict[str, int] = {
    "card_sber": 58899,
    "card_tbank": 59034,
    "nocomm": 58532,
    "phone": 59227,
}


def transfer_type_from_lines(lines: List[str]) -> str:
    if len(lines) > 4:
        return lines[4].strip()
    return ""


def classify_lines(lines: List[str], filename: str = "") -> str:
    text = "\n".join(lines)
    low = filename.lower()
    if low.startswith("сбп") or "Идентификатор операции" in text:
        return "sbp"
    if "Выписка" in text or "Движение средств" in text:
        return "statement"
    tt = transfer_type_from_lines(lines)
    if tt == "По номеру телефона":
        return "phone"
    if tt == "Клиенту Т-Банка":
        return "card_tbank"
    if tt == "По номеру карты":
        return "card_sber"
    if tt == "На карту":
        return "nocomm"
    return "unknown"


def classify_pdf(path: str) -> str:
    import fitz

    doc = fitz.open(path)
    lines = [l.strip() for l in doc[0].get_text().split("\n") if l.strip()]
    doc.close()
    return classify_lines(lines, os.path.basename(path))


def _iter_corpus_dirs() -> List[str]:
    """All existing corpus roots (SBP folder alone must not hide phone/card/nocomm)."""
    out: List[str] = []
    seen = set()
    for path in CORPUS_DIR_CANDIDATES:
        if not os.path.isdir(path):
            continue
        if not any(n.lower().endswith(".pdf") for n in os.listdir(path)):
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def corpus_paths(kind: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for root in _iter_corpus_dirs():
        for name in sorted(os.listdir(root)):
            if not name.lower().endswith(".pdf"):
                continue
            path = os.path.join(root, name)
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            try:
                if classify_pdf(path) != kind:
                    continue
            except Exception:
                continue
            seen.add(key)
            out.append(path)
    return out


def median_size(kind: str, fallback: Optional[int] = None) -> int:
    paths = corpus_paths(kind)
    if paths:
        sizes = [os.path.getsize(p) for p in paths]
        med = int(statistics.median(sizes))
        _SIZE_TARGET[kind] = med
        return med
    if kind in _SIZE_TARGET:
        return _SIZE_TARGET[kind]
    return fallback or 59000


def template_paths(kind: str, default_template: str) -> List[str]:
    """Шаблон проекта + PDF корпуса данного типа (для orig-pool)."""
    seen = set()
    out: List[str] = []
    for p in [default_template] + corpus_paths(kind):
        if p and os.path.isfile(p) and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def pick_donor(kind: str, op_date: Optional[str] = None) -> Optional[str]:
    paths = corpus_paths(kind)
    if not paths:
        return None
    if op_date:
        norm = lambda s: re.sub(r"\s+", " ", s.strip())
        want = norm(op_date)
        for p in paths:
            try:
                import fitz
                doc = fitz.open(p)
                head = doc[0].get_text().split("\n", 1)[0].strip()
                doc.close()
                if norm(head) == want:
                    return p
            except Exception:
                pass
    target = median_size(kind)
    return min(paths, key=lambda p: abs(os.path.getsize(p) - target))


def corpus_stats() -> Dict[str, Dict]:
    stats: Dict[str, Dict] = {}
    if not os.path.isdir(CORPUS_DIR):
        return stats
    buckets: Dict[str, List[int]] = {}
    for name in os.listdir(CORPUS_DIR):
        if not name.lower().endswith(".pdf"):
            continue
        path = os.path.join(CORPUS_DIR, name)
        try:
            kind = classify_pdf(path)
        except Exception:
            kind = "unknown"
        buckets.setdefault(kind, []).append(os.path.getsize(path))
    for kind, sizes in buckets.items():
        stats[kind] = {
            "label": _KIND_LABELS.get(kind, kind),
            "count": len(sizes),
            "size_min": min(sizes),
            "size_max": max(sizes),
            "size_median": int(statistics.median(sizes)),
        }
        _SIZE_TARGET[kind] = int(statistics.median(sizes))
    return stats


def _extract_text(path: str) -> str:
    import fitz
    doc = fitz.open(path)
    text = doc[0].get_text()
    doc.close()
    return text


def corpus_receipt_numbers(kind: str) -> List[str]:
    """Номера квитанций из реальных PDF корпуса."""
    seen: set = set()
    out: List[str] = []
    for path in corpus_paths(kind):
        try:
            text = _extract_text(path)
        except Exception:
            continue
        for m in _RECEIPT_NUM_RE.findall(text):
            if m not in seen:
                seen.add(m)
                out.append(m)
    return out


_SBP_OPID_CACHE: Optional[List[str]] = None


def corpus_sbp_operation_ids() -> List[str]:
    global _SBP_OPID_CACHE
    if _SBP_OPID_CACHE is not None:
        return list(_SBP_OPID_CACHE)
    seen: set = set()
    out: List[str] = []
    for path in corpus_paths("sbp"):
        try:
            text = _extract_text(path)
        except Exception:
            continue
        for m in _SBP_ID_RE.findall(text):
            u = m.upper()
            if len(u) == 27 and u not in seen:
                seen.add(u)
                out.append(u)
    _SBP_OPID_CACHE = out
    return list(out)


def normalize_receipt_num(raw: str, *, fallback: str = TBANK_RECEIPT_FALLBACK) -> str:
    """Формат T-Bank: 1-XXX-XXX-XXX-XXX (группы по 3 цифры после ведущей 1)."""
    s = str(raw or "").strip()
    if _RECEIPT_STRICT_RE.match(s):
        return s
    m = _RECEIPT_NUM_RE.search(s)
    if m:
        return m.group(0)
    parts = s.split("-")
    if len(parts) == 5 and parts[0] == "1":
        try:
            return (
                f"1-{int(parts[1]):03d}-{int(parts[2]):03d}-"
                f"{int(parts[3]):03d}-{int(parts[4]):03d}"
            )
        except ValueError:
            pass
    digits = re.sub(r"\D", "", s)
    if digits.startswith("1") and len(digits) >= 13:
        d = digits[:13]
        return f"1-{d[1:4]}-{d[4:7]}-{d[7:10]}-{d[10:13]}"
    return fallback


def _known_receipt_stems() -> set[str]:
    """Stem'ы 1-XXX-XXX-XXX из atlas — их нельзя переиспользовать с новой датой/opid."""
    try:
        import json
        from pathlib import Path
        atlas = (
            Path(r"C:\Users\fanis\OneDrive\Desktop\pdf-checker-bot")
            / "detector" / "atlas_data" / "tbank_sbp_epoch_stems.json"
        )
        if not atlas.is_file():
            return set()
        data = json.loads(atlas.read_text(encoding="utf-8"))
        return set((data.get("receipt_stems") or {}).keys())
    except Exception:
        return set()


def _rand_receipt_bcd(*, forbid: str = "", avoid_stem: str = "") -> str:
    """1-132-B-C-D: фиксированный A=132, B/C/D — случайные тройки."""
    bad = set(forbid)
    known = _known_receipt_stems()
    seed = int.from_bytes(secrets.token_bytes(8), "big")
    import random as _rnd

    rng = _rnd.Random(seed)
    a = TBANK_RECEIPT_A
    for _ in range(1024):
        b = f"{rng.randint(0, 999):03d}"
        c = f"{rng.randint(0, 999):03d}"
        d = f"{rng.randint(0, 999):03d}"
        if b in bad or c in bad or d in bad:
            continue
        stem = f"1-{a}-{b}-{c}"
        if stem in known:
            continue
        if avoid_stem and stem == avoid_stem:
            continue
        return f"{stem}-{d}"
    return (
        f"1-{a}-{rng.randint(0, 999):03d}-"
        f"{rng.randint(0, 999):03d}-{rng.randint(0, 999):03d}"
    )


def remix_receipt_d_only(base: str, *, forbid: str = "") -> str:
    """Авто-квитанция: всегда 1-132- + новый B-C-D (base игнорируется)."""
    _ = base
    return _rand_receipt_bcd(forbid=forbid)


def remix_receipt_tail(base: str, *, forbid: str = "") -> str:
    """Авто-квитанция: всегда 1-132- + новый B-C-D (base игнорируется)."""
    _ = base
    return _rand_receipt_bcd(forbid=forbid)


def gen_receipt_number(
    kind: str,
    *,
    op_date: Optional[str] = None,
    forbid: str = "",
    fallback: str = TBANK_RECEIPT_FALLBACK,
) -> str:
    """Квитанция Т-Банка: всегда 1-132-XXX-XXX-XXX (B/C/D рандом)."""
    _ = kind, op_date  # kind/date больше не выбирают чужой префикс корпуса
    return normalize_receipt_num(
        _rand_receipt_bcd(forbid=forbid),
        fallback=fallback,
    )


def gen_sbp_operation_id(
    date_str: str,
    *,
    bank: str = "",
    amount: str = "",
    phone: str = "",
    account: str = "",
    receiver: str = "",
) -> str:
    """SBP ID: скелет с реального чека + время операции внутри номера."""
    from datetime import datetime as _dt, timedelta as _td
    import hashlib as _hl

    try:
        clean = re.sub(r"\s+", " ", date_str.strip())
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                dt_msk = _dt.strptime(clean, fmt)
                break
            except ValueError:
                dt_msk = None
        if dt_msk is None:
            raise ValueError("bad date")
    except ValueError:
        from tbank_sbp_stealth import _gen_sbp_id
        return _gen_sbp_id(date_str, bank=bank, amount=amount, phone=phone,
                           account=account, receiver=receiver)

    pool = corpus_sbp_operation_ids()
    if not pool:
        from tbank_sbp_stealth import _gen_sbp_id
        return _gen_sbp_id(date_str, bank=bank, amount=amount, phone=phone,
                           account=account, receiver=receiver)

    seed = int.from_bytes(secrets.token_bytes(8), "big")
    import random as _rnd
    rng = _rnd.Random(seed)
    template = rng.choice(pool)

    from tbank_sbp_stealth import (
        decode_sbp_operation_id,
        _gen_sbp_id,
        _gen_sbp_suffix,
        ensure_sbp_class_suffix_consistency,
    )
    dec = decode_sbp_operation_id(template)
    if not dec:
        return _gen_sbp_id(date_str, bank=bank, amount=amount, phone=phone,
                           account=account, receiver=receiver)

    dt_utc = dt_msk - _td(hours=3)
    doy = dt_utc.timetuple().tm_yday
    year_dig = dt_utc.year - 2020
    fp = f"{clean}|{phone}|{amount}|{account}|{bank}|{receiver}|{template}"
    hm = _hl.md5(fp.encode("utf-8")).digest()
    # Preserve route_marker ID[14] alphabet — never let %10000 spill a '2'..'9' there.
    from tbank_sbp_stealth import (
        _SBP_LINKED_ROUTE_ALPHABET,
        _finalize_sbp_identity,
        _gen_sbp_suffix,
    )
    ref3 = f"{(int(dec['ref4'][:3]) + hm[0]) % 1000:03d}"
    suffix = _gen_sbp_suffix(bank, amount)
    # Guess class from template channel/bank for alphabet; finalize will harden.
    ch = dec.get("channel") or "1014"
    bsuf = dec.get("bank_code") or "00117"
    cls_guess = "B1" if str(ch)[:1] == "1" and bsuf.endswith("117") else (
        "B0" if str(ch) == "0016" else "G1"
    )
    alphabet = _SBP_LINKED_ROUTE_ALPHABET.get((cls_guess, bsuf), "01")
    mark = alphabet[hm[3] % len(alphabet)]
    ref4 = ref3 + mark
    ss = min(59, dt_utc.second + (hm[1] & 1))

    l1 = dec["l1"]
    if not l1.isalpha():
        l1 = "A"
    sbp_id = (
        f"{dec['prefix']}"
        f"{year_dig * 1000 + doy:04d}"
        f"{dt_utc.hour:02d}"
        f"{dt_utc.minute:02d}"
        f"{ss:02d}"
        f"{ref4}"
        f"{l1}"
        f"0"
        f"{dec['l2']}"
        f"{dec['channel']}"
        f"{dec['bank_code']}"
    )
    if len(sbp_id) != 27:
        return _gen_sbp_id(date_str, bank=bank, amount=amount, phone=phone,
                           account=account, receiver=receiver)
    return _finalize_sbp_identity(sbp_id, suffix, bank=bank, amount=amount)
