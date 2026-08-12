"""
Корпус оригиналов Сбербанка — классификация подметодов.

Пути: ~/Desktop/чеки/сбер + templates/sber_shells
"""
from __future__ import annotations

import os
import re
import secrets
import statistics
from datetime import datetime, timedelta
from time_msk import now_msk

CORPUS_DIR = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "сбер")
_SHELLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "sber_shells")

_SBER_SBP_ID_RE = re.compile(r"\bA[0-9]{14}[0-9]0G[0-9]{4}0011[67][0-9]{5}\b")

_KIND_LABELS = {
    "sbp": "СБП",
    "phone": "По номеру телефона",
    "card_other": "По карте в другой банк",
    "unknown": "неизвестно",
}

_SIZE_TARGET: Dict[str, int] = {
    "sbp": 102487,
    "phone": 44500,
    "card_other": 38736,
}


def _extract_text(path: str) -> str:
    import fitz
    doc = fitz.open(path)
    text = doc[0].get_text()
    doc.close()
    return text


def classify_lines(lines: List[str], filename: str = "") -> str:
    text = "\n".join(lines)
    low = filename.lower()
    if "сбп" in low or "спб" in low or "перевод по сбп" in text.lower():
        return "sbp"
    if "по номеру телефона" in text.lower() or "телефон" in low and "карт" not in low:
        if "телефон получателя" in text or "по номеру телефона" in text.lower():
            return "phone"
    if "на карту" in text.lower() or "карт" in low:
        return "card_other"
    sz_hint = 0
    try:
        sz_hint = os.path.getsize(filename) if filename and os.path.isfile(filename) else 0
    except Exception:
        pass
    if 100000 <= sz_hint <= 104500:
        return "sbp"
    if 43000 <= sz_hint <= 46000:
        return "phone"
    if 35000 <= sz_hint <= 40000:
        return "card_other"
    return "unknown"


def classify_pdf(path: str) -> str:
    lines = [l.strip() for l in _extract_text(path).split("\n") if l.strip()]
    return classify_lines(lines, path)


def _all_roots() -> List[str]:
    roots = []
    for p in [_SHELLS_DIR, CORPUS_DIR]:
        if os.path.isdir(p):
            roots.append(p)
    return roots


def corpus_paths(kind: str) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for root in _all_roots():
        for name in sorted(os.listdir(root)):
            if not name.lower().endswith(".pdf"):
                continue
            path = os.path.join(root, name)
            if path in seen:
                continue
            try:
                if classify_pdf(path) == kind:
                    seen.add(path)
                    out.append(path)
            except Exception:
                pass
    if kind == "sbp" and not out:
        for root in _all_roots():
            for name in sorted(os.listdir(root)):
                if not name.lower().endswith(".pdf"):
                    continue
                path = os.path.join(root, name)
                if path in seen:
                    continue
                sz = os.path.getsize(path)
                if 100000 <= sz <= 104500:
                    seen.add(path)
                    out.append(path)
    return out


def median_size(kind: str, fallback: Optional[int] = None) -> int:
    paths = corpus_paths(kind)
    if paths:
        sizes = [os.path.getsize(p) for p in paths]
        med = int(statistics.median(sizes))
        _SIZE_TARGET[kind] = med
        return med
    return _SIZE_TARGET.get(kind) or fallback or 102487


def corpus_sbp_operation_ids() -> List[str]:
    seen: set = set()
    out: List[str] = []
    for path in corpus_paths("sbp"):
        try:
            text = _extract_text(path)
        except Exception:
            continue
        for m in _SBER_SBP_ID_RE.findall(text):
            u = m.upper()
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out


def decode_sber_sbp_id(sbp_id: str) -> Optional[Dict[str, str]]:
    s = re.sub(r"\s+", "", (sbp_id or "").strip().upper())
    if len(s) != 32 or s[0] != "A" or s[16] != "0" or s[17] != "G":
        return None
    if s[22:27] not in ("00116", "00117"):
        return None
    return {
        "prefix": s[0],
        "doy4": s[1:5],
        "utc_hour": s[5:7],
        "utc_minute": s[7:9],
        "utc_second": s[9:11],
        "ref4": s[11:15],
        "l1": s[15],
        "zero": s[16],
        "letter": s[17],
        "channel": s[18:22],
        "bank_code": s[22:27],
        "suffix5": s[27:32],
        "raw": s,
    }


def _parse_dt(date_in: str, time_in: str = "") -> datetime:
    date_in = (date_in or "").strip()
    time_in = (time_in or "").strip()
    if not time_in:
        time_in = now_msk().strftime("%H:%M:%S")
    elif len(time_in) == 5:
        time_in = f"{time_in}:00"
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(f"{date_in} {time_in}", fmt)
        except ValueError:
            continue
    return now_msk().replace(microsecond=0)


def remix_sbp_suffix(base: str) -> str:
    """Меняем только последние 5 цифр SBP ID (как хвост квитанции у T-Bank)."""
    dec = decode_sber_sbp_id(base)
    if not dec:
        return base
    old = dec["suffix5"]
    seed = int.from_bytes(secrets.token_bytes(8), "big")
    import random as _rnd
    rng = _rnd.Random(seed)
    for _ in range(128):
        tail = f"{rng.randint(0, 99999):05d}"
        if tail != old:
            return dec["raw"][:27] + tail
    return dec["raw"][:27] + f"{(int(old) + 1) % 100000:05d}"


def gen_sbp_operation_id(
    date_in: str,
    *,
    time_in: str = "",
    bank: str = "",
    amount: str = "",
    phone: str = "",
    account: str = "",
    receiver: str = "",
) -> str:
    """Sber SBP ID: скелет с реального чека + время операции внутри."""
    import hashlib as _hl

    pool = corpus_sbp_operation_ids()
    dt_msk = _parse_dt(date_in, time_in)

    if not pool:
        from sber_sbp_stealth import _generate_sbp_number
        return _generate_sbp_number(
            bank, date_in, time_in=time_in, amount=amount,
            phone=phone, account=account, receiver=receiver,
        )

    seed = int.from_bytes(secrets.token_bytes(8), "big")
    import random as _rnd
    rng = _rnd.Random(seed)
    template = rng.choice(pool)
    dec = decode_sber_sbp_id(template)
    if not dec:
        from sber_sbp_stealth import _generate_sbp_number
        return _generate_sbp_number(
            bank, date_in, time_in=time_in, amount=amount,
            phone=phone, account=account, receiver=receiver,
        )

    dt_utc = dt_msk - timedelta(hours=3)
    from sber_sbp_stealth import _sber_core_utc
    core = _sber_core_utc(dt_msk)
    doy = core.timetuple().tm_yday
    year_dig = core.year - 2020
    fp = f"{dt_msk.strftime('%d.%m.%Y %H:%M:%S')}|{phone}|{amount}|{account}|{bank}|{receiver}|{template}"
    hm = _hl.md5(fp.encode("utf-8")).digest()
    ref4 = f"{(int(dec['ref4']) + hm[0]) % 10000:04d}"
    suffix = remix_sbp_suffix(template)[27:32]

    sbp_id = (
        f"{dec['prefix']}"
        f"{year_dig * 1000 + doy:04d}"
        f"{core.hour:02d}"
        f"{core.minute:02d}"
        f"{core.second:02d}"
        f"{ref4}"
        f"{dec['l1']}"
        f"0"
        f"{dec['letter']}"
        f"{dec['channel']}"
        f"{dec['bank_code']}"
        f"{suffix}"
    )
    if len(sbp_id) != 32:
        from sber_sbp_stealth import _generate_sbp_number
        return _generate_sbp_number(
            bank, date_in, time_in=time_in, amount=amount,
            phone=phone, account=account, receiver=receiver,
        )
    return sbp_id


_DOC_NUM_RE = re.compile(r"\b\d{19}\b")


def corpus_document_numbers(kind: str = "phone") -> List[str]:
    seen: set = set()
    out: List[str] = []
    for path in corpus_paths(kind):
        try:
            text = _extract_text(path)
        except Exception:
            continue
        for m in _DOC_NUM_RE.findall(text):
            if m not in seen:
                seen.add(m)
                out.append(m)
    return out


def remix_document_tail(base: str) -> str:
    base = re.sub(r"\D", "", base)
    if len(base) != 19:
        return base
    old = base[-5:]
    seed = int.from_bytes(secrets.token_bytes(8), "big")
    import random as _rnd
    rng = _rnd.Random(seed)
    for _ in range(128):
        tail = f"{rng.randint(0, 99999):05d}"
        if tail != old:
            return base[:14] + tail
    return base[:14] + f"{(int(old) + 1) % 100000:05d}"


def gen_document_number(kind: str = "phone", fallback: str = "1000000005312075311") -> str:
    pool = corpus_document_numbers(kind)
    if not pool:
        return remix_document_tail(fallback)
    seed = int.from_bytes(secrets.token_bytes(8), "big")
    import random as _rnd
    return remix_document_tail(_rnd.Random(seed).choice(pool))


def template_paths(kind: str, default_template: str) -> List[str]:
    seen: set = set()
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


def merged_cid_map(kind: str, shell_path: str) -> Dict[str, int]:
    """Объединённый charset: shell + все PDF корпуса подметода."""
    from sber_dynamic import _cid_map_from_pdf
    out = dict(_cid_map_from_pdf(shell_path))
    for path in corpus_paths(kind):
        try:
            out.update(_cid_map_from_pdf(path))
        except Exception:
            pass
    return out
