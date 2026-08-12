# -*- coding: utf-8 -*-
"""Sequential Proton queue: 1 method at a time, ~3-4 checks/min, visual gate.

- Diverse FIO + large amounts (not donor clones)
- Reject before TG if glyphs missing or text mangled
- Fast TG wait; no long remainder sleep; avoid flood TIMEOUT waste
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
_CHECKER = Path(r"C:\Users\fanis\OneDrive\Desktop\pdf-checker-bot")
_OUT = _ROOT / "output" / "night_dual"
_STOP = _OUT / "STOP"
_STOP_FAKE = _OUT / "STOP_FAKE.json"
_POS = _OUT / "queue_pos.json"
_CHARSET_STATUS = _OUT / "charset_status.json"
_FAKE_FP = _OUT / "fake_fingerprints.json"
_QUARANTINE = _OUT / "quarantine.json"
_BAKE_COOLDOWN: Dict[str, float] = {}
_PASS_STREAK: Dict[str, int] = {}
_FAKE_QUARANTINE_N = 5  # same fingerprint → quarantine method this round

sys.path = [p for p in sys.path if "vendor" not in p.replace("\\", "/").lower()]
for p in (_CHECKER, _DIR, _ROOT, _CHECKER):
    if p and p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from onlypdf_safe_names import (  # noqa: E402
    CYR_NO_YO_TVERD,
    FEMALE,
    MALE,
    RECV_SHORT,
    SBER_FIO_LONG,
    SBER_FIO_SHORT,
    STRESS_LONG_RECV,
    STRESS_LONG_SENDERS,
    UNUSUAL_SENDERS,
)
from onlypdf_gate import (  # noqa: E402
    DEFAULT_PROTON,
    _norm_bot,
    _parse_proton,
    _wait_verdict,
    open_onlypdf_client,
)

QUEUE = [
    "tbank_sbp",
    "tbank_card_sber",
    "tbank_card_tbank",
    "tbank_phone",
    "tbank_nocomm",
    "alfa_sbp",
    "alfa_card",
    "alfa_phone",
    "sber_sbp",
    "sber_phone",
]


def _load_gens():
    from tbank_sbp_stealth import create_tbank_sbp_stealth
    from tbank_stealth_v3 import create_tbank_stealth
    from tbank_card_tbank_stealth import create_tbank_card_tbank_stealth
    from tbank_phone_stealth import create_tbank_phone_stealth
    from tbank_nocomm_stealth import create_tbank_nocomm_stealth
    from alfa_sbp_stealth import create_alfa_sbp_stealth
    from alfa_card_stealth import create_alfa_card_stealth
    from alfa_phone_stealth import create_alfa_phone_stealth
    from sber_sbp_stealth import create_sber_sbp_stealth
    from sber_phone_stealth import create_sber_phone_stealth

    return {
        "tbank_sbp": create_tbank_sbp_stealth,
        "tbank_card_sber": create_tbank_stealth,
        "tbank_card_tbank": create_tbank_card_tbank_stealth,
        "tbank_phone": create_tbank_phone_stealth,
        "tbank_nocomm": create_tbank_nocomm_stealth,
        "alfa_sbp": create_alfa_sbp_stealth,
        "alfa_card": create_alfa_card_stealth,
        "alfa_phone": create_alfa_phone_stealth,
        "sber_sbp": create_sber_sbp_stealth,
        "sber_phone": create_sber_phone_stealth,
    }


def _log(msg: str) -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write((line + "\n").encode(enc, errors="replace"))
        sys.stdout.flush()
    with (_OUT / "live.log").open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def _append(name: str, row: dict) -> None:
    with (_OUT / name).open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False) + "\n")


def _save_pos(idx: int, method: str, note: str = "") -> None:
    _POS.write_text(
        json.dumps(
            {"idx": idx, "method": method, "note": note, "ts": datetime.now().isoformat()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# Qualified Cyrillic + digits.  The donor corpus does not consistently cover
# ъ/ё/й; these letters are excluded rather than substituted.
_ALL_CYR_LOWER = "абвгдежзиклмнопрстуфхцчшщыьэюя"
_ALL_CYR = _ALL_CYR_LOWER + _ALL_CYR_LOWER.upper()
_DIG = "0123456789"
# Phone Regular has no atlas-safe Ш/Щ/ъ — soft-cover like the generator.
_PHONE_ATLAS_UNSAFE = str.maketrans({"Ш": "С", "Щ": "Ч", "ъ": "ь"})
# Keyboard-row mash (as in bot stress pastes) + rare clusters
_MASH_ROWS = (
    "цукенгшщзх",
    "фывапролджэ",
    "ячсмитьбю",
    "ыэщжцшхчю",
)


def _title_word(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "А"
    return s[0].upper() + s[1:].lower()


def _word_cover(rng: random.Random, alphabet: str, n: int, *, start: int = 0) -> str:
    """Long token covering alphabet slice — shuffled so CIDs aren't ascending."""
    letters = list(alphabet)
    if not letters:
        return "А" * max(1, n)
    n = max(n, 8)
    rot = letters[start % len(letters):] + letters[: start % len(letters)]
    # Pick with replacement from rotated alphabet, then shuffle — covers charset
    # without a monotonic CID ladder in one Tj (Proton HARD ≥6 ascending).
    base_chars = [rot[i % len(rot)] for i in range(n)]
    rng.shuffle(base_chars)
    base = "".join(base_chars)
    if rng.random() < 0.55:
        mash = rng.choice(_MASH_ROWS)
        pos = rng.randint(0, max(0, n - 4))
        chunk = "".join(rng.choice(mash) for _ in range(min(6, n // 3)))
        base = base[:pos] + chunk + base[pos + len(chunk):]
        # reshuffle again after mash insert
        tmp = list(base[:n])
        rng.shuffle(tmp)
        base = "".join(tmp)
    return _title_word(base[:n])


_NO_START = set("ьъыЬЪЫ")  # Proton SBER_FIO_YERY_INITIAL / invalid word onset


# T-Bank F1 must stay a corpus SHA-twin FontFile2. Alphabet = exact co-occur
# set of lean twin сбп56 (cmap=68, glyf=12554): labels+digits+ВТБ/Сбер/Совком.
# Broader union alphabets force fat twins (71/76) that need Td -500 off-page
# → CONTENT_TD_SENTINEL / TR_MODE HARD. No х (absent on сбп56).
_TBANK_TWIN_SAFE_LOWER = "абвгдежзийклмнопрстуфцчшщьюя"
# Title-case starts from сбп56 ToUnicode only (no ДЖМ — those force other twins).
_TBANK_TWIN_SAFE_INITIALS = "БВИКОПСТУ"
_TBANK_TWIN_SAFE_GAZ_INITIALS = "БВГДИКМОПСТУ"
# сбп8 co-occur (has А; no В/Г) — pair only with Альфа/Сбер/Совком.
_TBANK_TWIN_SAFE_ALFA_LOWER = "абвгдежзийклмнопрстуфцчшщьюя"
_TBANK_TWIN_SAFE_ALFA_INITIALS = "АБДИКОПРСТУХ"


def _chaos_token(
    rng: random.Random, n: int, *, mixed: bool = False, alphabet: str | None = None,
) -> str:
    """Keyboard-mash token like «Факпвр» / «Ортмимим» — full alphabet stress.

    ``mixed`` kept for T-Bank/Alfa. Sber format HARD needs Title case only.
    Never start with Ы/Ь/Ъ (SBER_FIO_YERY_INITIAL).
    """
    n = max(1, n)
    alphabet = list(alphabet or _ALL_CYR_LOWER)
    alpha_set = set(alphabet)
    mash = [c for c in rng.choice(_MASH_ROWS) if c in alpha_set] or list(alphabet)
    start_alphabet = [c for c in alphabet if c not in _NO_START] or alphabet
    chars: List[str] = []
    for k in range(n):
        pool = start_alphabet if k == 0 else alphabet
        mash_pool = [c for c in mash if (k > 0 or c not in _NO_START)] or list(pool)
        if rng.random() < 0.55:
            chars.append(rng.choice(mash_pool))
        else:
            chars.append(rng.choice(pool))
    # Shuffle tail only — keep legal onset.
    if len(chars) > 2:
        tail = chars[1:]
        rng.shuffle(tail)
        chars = [chars[0]] + tail
    if chars[0] in _NO_START:
        chars[0] = rng.choice(start_alphabet)
    if mixed and n >= 4:
        out = [chars[0].upper()]
        for k, ch in enumerate(chars[1:], start=1):
            if k < rng.randint(1, 3) or (rng.random() < 0.18):
                out.append(ch.upper())
            else:
                out.append(ch.lower())
        return "".join(out)
    return chars[0].upper() + "".join(chars[1:]).lower()


def _mash_word(rng: random.Random, i: int, attempt: int, *, n: int) -> str:
    """One mash title-case word from keyboard rows + rare letters."""
    row = list(_MASH_ROWS[(i + attempt) % len(_MASH_ROWS)])
    rare = [c for c in _ALL_CYR_LOWER if c not in "".join(_MASH_ROWS)]
    if rare:
        row = row + rare
    chars = [rng.choice(row) for _ in range(max(1, n))]
    if chars[0] in _NO_START:
        chars[0] = rng.choice([c for c in row if c not in _NO_START] or list("абвгд"))
    return _title_word("".join(chars))


def _mash_fio(
    rng: random.Random,
    i: int,
    attempt: int,
    *,
    shape: str = "two",
) -> str:
    """Keyboard-row FIO — full alphabet, no soft-cover / dictionary names.

    shape:
      two      — «Имя Отчество» (default T-Bank / Alfa SBP)
      sber_recv — «Имя Отчество И» (Proton SBER_SBP_RECIPIENT_FIO_FORMAT)
      sber_send — «Имя Отчество И.» (Proton SBER_SBP_SENDER_FIO_FORMAT)
      alfa_phone — «Фам**в Д. В.» (two initials + surname mask)
    """
    if shape in ("sber_recv", "sber_send"):
        # Fit Jasper slots (~37–43 B): short mash tokens, full alphabet.
        a = _mash_word(rng, i, attempt, n=rng.randint(4, 6))
        b = _mash_word(rng, i + 1, attempt + 1, n=rng.randint(6, 9))
        ini_pool = [c for c in _ALL_CYR_LOWER if c not in _NO_START] or list("абвгдеж")
        ini = rng.choice(ini_pool).upper()
        if shape == "sber_recv":
            return f"{a} {b} {ini}"
        return f"{a} {b} {ini}."
    a = _mash_word(rng, i, attempt, n=rng.randint(6, 10))
    b = _mash_word(rng, i + 1, attempt + 1, n=rng.randint(8, 12))
    ini_pool = [c for c in _ALL_CYR_LOWER if c not in _NO_START] or list("абвгдеж")
    ini = rng.choice(ini_pool).upper()
    if shape == "alfa_phone":
        # Masked surname + two initials (corpus phone shape).
        core = a if len(a) >= 4 else (a + b)[:6]
        masked = core[:3] + "**" + core[-1]
        ini2 = rng.choice(ini_pool).upper()
        return f"{masked} {ini}. {ini2}."
    # two — optional third initial with period (T-Bank-ish), still mash letters.
    if rng.random() < 0.35:
        return f"{a} {b} {ini}."
    return f"{a} {b}"


def _stress_fio(
    rng: random.Random,
    i: int,
    attempt: int,
    *,
    twin_safe: bool = False,
    prefer_mash_rows: bool = False,
    twin_profile: str = "sbp48",
) -> str:
    """Chaos FIO — full Cyrillic by default (all letters). Prefer mash shape."""
    if not twin_safe:
        return _mash_fio(rng, i, attempt)
    # twin_safe: co-occur letters on one corpus SHA twin (+ digits/labels).
    if twin_profile == "sbp8":
        alpha = list(_TBANK_TWIN_SAFE_ALFA_LOWER)
        ini = list(_TBANK_TWIN_SAFE_ALFA_INITIALS)
    elif twin_profile == "sbp48_gaz":
        alpha = list(_TBANK_TWIN_SAFE_LOWER)
        ini = list(_TBANK_TWIN_SAFE_GAZ_INITIALS)
    else:
        alpha = list(_TBANK_TWIN_SAFE_LOWER)
        ini = list(_TBANK_TWIN_SAFE_INITIALS)
    parts: List[str] = []
    for p in range(2):
        n = rng.choice([6, 7, 8, 9])
        body = "".join(rng.choice(alpha) for _ in range(max(1, n - 1)))
        parts.append(rng.choice(ini) + body)
    if rng.random() < 0.30:
        parts.append(rng.choice(ini) + ".")
    return " ".join(parts)


def _stress_sber_sender(rng: random.Random, i: int, attempt: int) -> str:
    """Sber SBP: short common FIO with patronymic + initial (donor slot shape)."""
    firsts = ("Иван", "Петр", "Олег", "Павел", "Денис", "Роман", "Антон", "Борис")
    patrs = (
        "Иванович", "Петрович", "Олегович", "Павлович",
        "Денисович", "Романович", "Антонович", "Борисович",
    )
    initials = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯ"
    return (
        f"{firsts[(i + attempt) % len(firsts)]} "
        f"{patrs[(i * 3 + attempt) % len(patrs)]} "
        f"{initials[(i * 5 + attempt) % len(initials)]}."
    )


def _corpus_tbank_face(i: int, attempt: int, *, method: str = "tbank_sbp") -> dict:
    """Full face from one corpus PDF — donor-orig equal-CID / Proton PASS path.

    Card channels must NOT pull SBP faces (Евгений/Инга) — those letters are
    absent from h=471 F1 twins and force hydrate → GLYF_CMAP_EXACT FAKE.
    """
    import fitz

    fallback = {
        "sender": "Степан Некрасов",
        "receiver": "Дмитрий М.",
        "amount": "9700",
        "phone": "+7 (940) 777-06-13",
        "bank": "Сбербанк",
        "card": "427655******4505",
    }
    if method in ("tbank_card_sber", "tbank_card_tbank", "tbank_nocomm"):
        try:
            from tbank_corpus import corpus_paths
            from tbank_stealth_v3 import _extract_card_fields

            kind = (
                "card_tbank"
                if method == "tbank_card_tbank"
                else "card_sber"
            )
            paths = list(corpus_paths(kind) or [])
            if not paths:
                return fallback
            # Prefer zero-commission faces — nonzero net/gross slots break
            # donor-orig flate far more often (GEN_NONE → dynamic reject).
            scored: list[tuple[int, str, dict]] = []
            for pth in paths:
                o = _extract_card_fields(pth)
                if not o:
                    continue
                comm = (o.get("commission") or "").strip()
                score = 0
                if comm.startswith("0") or "Без" in comm:
                    score += 10
                if (o.get("date") or "").strip() >= "29.06.2026":
                    score += 3
                scored.append((score, pth, o))
            if not scored:
                return fallback
            scored.sort(key=lambda t: (-t[0], t[1]))
            # Rotate among top-scoring donors.
            top = [row for row in scored if row[0] == scored[0][0]] or scored
            _score, _path, o = top[(i * 3 + attempt) % len(top)]
            amt = re.sub(r"[^\d].*", "", (o.get("amount") or "").replace(" ", ""))
            return {
                "sender": o.get("sender") or fallback["sender"],
                "receiver": o.get("receiver") or fallback["receiver"],
                "amount": amt or fallback["amount"],
                "phone": fallback["phone"],
                "bank": o.get("bank") or "Сбербанк",
                "card": o.get("card") or fallback["card"],
                "date": (o.get("date") or "").strip(),
            }
        except Exception:
            return fallback

    root = _ROOT / "templates" / "tbank_sbp_corpus"
    pdfs = sorted(root.glob("*.pdf")) if root.is_dir() else []
    sbp_fallback = {
        "sender": "Евгений Дектерев",
        "receiver": "Инга Д.",
        "amount": "9700",
        "phone": "+7 (940) 777-06-13",
        "bank": "Сбербанк",
    }
    if not pdfs:
        return sbp_fallback
    path = pdfs[(i * 3 + attempt) % len(pdfs)]
    try:
        doc = fitz.open(path)
        lines = [ln.strip() for ln in doc[0].get_text().splitlines() if ln.strip()]
        doc.close()
        s = lines[lines.index("Отправитель") - 1]
        r = lines[lines.index("Получатель") - 1]
        if s.startswith("+7") or r.startswith("+7"):
            raise ValueError("phone misparsed as FIO")
        phone = next((ln for ln in lines if ln.startswith("+7")), sbp_fallback["phone"])
        bank = "Сбербанк"
        try:
            bank = lines[lines.index("Банк получателя") + 1]
        except Exception:
            pass
        amount = sbp_fallback["amount"]
        for ln in lines:
            if re.match(r"^\d", ln) and "****" not in ln and len(ln) < 14 and "," not in ln:
                amount = re.sub(r"\s*i\s*$", "", ln).replace(" ", "")
                break
        return {
            "sender": s,
            "receiver": r,
            "amount": amount,
            "phone": phone,
            "bank": bank,
        }
    except Exception:
        return sbp_fallback


def _corpus_tbank_fio_pair(
    rng: random.Random, i: int, attempt: int, *, method: str = "tbank_sbp",
) -> tuple[str, str]:
    face = _corpus_tbank_face(i, attempt, method=method)
    return face["sender"], face["receiver"]


def _corpus_alfa_face(i: int, attempt: int, *, method: str = "alfa_sbp") -> dict:
    """Full face from one Alfa corpus PDF — twin-shell Proton PASS path.

    Proton alfa_v2 2.1.0 HARD on ALFA_CONTENT_BODY_EXACT_UNKNOWN: any content
    rewrite fails. War must reuse a genuine face so the generator ships the
    unmodified corpus body (+ fresh trailer /ID).
    """
    fallback = {
        "receiver": "Алина Александровна А",
        "amount": "4000",
        "phone": "+7 (927) 489-03-91",
        "bank": "Озон Банк (Ozon)",
        "account": "40817810405614696324",
        "date_time": "04.06.2026 18:45:37",
        "operation_num": "C160406261736948",
        "sbp_id": "A61551545348731O0G10080011770901",
        "_alfa_twin_path": "",
    }
    try:
        from alfa_corpus import canonical_paths, corpus_paths
        from alfa_sbp_stealth import _extract_sbp_fields

        if method == "alfa_phone":
            # Phone channel has its own donors; keep SBP twin for sbp/card stress.
            paths = list(canonical_paths("phone") or []) or list(corpus_paths("phone") or [])
        else:
            paths = list(canonical_paths("sbp") or []) or list(corpus_paths("sbp") or [])
        if not paths:
            return fallback
        path = paths[(i * 3 + attempt) % len(paths)]
        fields = _extract_sbp_fields(path)
        if not fields:
            return fallback
        amt = re.sub(r"\D", "", (fields.get("amount") or "").replace("\xa0", " "))
        # Drop trailing RUR noise — keep ruble integer digits only.
        if amt.endswith("00") and len(amt) > 4:
            # amounts are whole rubles in face; do not strip
            pass
        phone = (fields.get("phone") or fallback["phone"]).replace("\xa0", " ").strip()
        dt = (fields.get("date_time") or "").replace("\xa0", " ")
        dt = re.sub(r"\s*мск\s*$", "", dt, flags=re.I).strip()
        op = (fields.get("operation_num") or "").replace("\xa0", " ").strip()
        sbp = (fields.get("sbp_id") or "").replace("\xa0", " ").strip()
        bank = (fields.get("recipient_bank") or fallback["bank"]).replace("\xa0", " ").strip()
        acct = re.sub(r"\D", "", fields.get("account") or "")
        recv = (fields.get("receiver") or fallback["receiver"]).replace("\xa0", " ").strip()
        return {
            "receiver": recv,
            "amount": amt or fallback["amount"],
            "phone": phone,
            "bank": bank,
            "account": acct or fallback["account"],
            "date_time": dt or fallback["date_time"],
            "operation_num": op or fallback["operation_num"],
            "sbp_id": sbp or fallback["sbp_id"],
            "_alfa_twin_path": path if method == "alfa_sbp" else "",
        }
    except Exception:
        return fallback



def _stress_tbank_sender(
    rng: random.Random, i: int, attempt: int, *, method: str = "tbank_sbp",
) -> str:
    """T-Bank: prefer corpus face FIO (skeleton-safe equal-CID); rare mash."""
    if rng.random() < 0.05:
        return _stress_fio(rng, i, attempt, twin_safe=False)
    return _corpus_tbank_fio_pair(rng, i, attempt, method=method)[0]


def _stress_recv(rng: random.Random, i: int, attempt: int, *, style: str) -> str:
    if style in ("tbank", "short"):
        if rng.random() < 0.05:
            return _stress_fio(rng, i + 3, attempt, twin_safe=False)
        return _corpus_tbank_fio_pair(rng, i, attempt)[1]
    if style in ("sber", "long"):
        firsts = ("Иван", "Петр", "Олег", "Мария", "Анна", "Елена", "Ольга", "Павел")
        patrs = (
            "Иванович", "Петрович", "Олегович", "Ивановна", "Петровна",
            "Павловна", "Денисовна", "Антоновна",
        )
        initials = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯ"
        return (
            f"{firsts[(i + attempt) % len(firsts)]} "
            f"{patrs[(i * 3 + attempt) % len(patrs)]} "
            f"{initials[(i + attempt * 7) % len(initials)]}"
        )
    return _stress_fio(rng, i + 90, attempt + 2)


def _stress_amount(rng: random.Random, i: int, attempt: int, *, method: str) -> str:
    """Diverse amounts — prefer 4–6 digits (real receipt scale), rare tiny.

    Too many 1–3 digit heads never exercise Medium digit glyfs / F2 HARD.
    """
    digs = _DIG
    if method.startswith("sber"):
        nlen = 4  # «3500.00»-class — no lead-space pad, flate-stable
    else:
        # Bias: mostly thousands–hundreds of thousands; tiny only ~10%.
        nlen = rng.choice([4, 4, 4, 5, 5, 5, 5, 6, 6, 3, 2])
    body = [rng.choice(digs) for _ in range(nlen)]
    if body[0] == "0":
        body[0] = rng.choice([d for d in digs if d != "0"])
    body[(i + attempt) % len(body)] = digs[(i + attempt) % 10]
    n = int("".join(body)) or rng.randint(1000, 9999)
    if method.startswith("sber"):
        n = max(1000, min(n, 9999))
    else:
        # Rotate through fat anchors so every digit 0–9 hits Medium often.
        fat = (
            1500, 2500, 7800, 9999, 12500, 22024, 35000, 50000,
            67890, 93934, 123456, 250000, 480000, 750000, 999999,
            10500, 44800, 81234, 156789, 320150,
        )
        roll = rng.random()
        if roll < 0.55:
            n = fat[(i * 3 + attempt) % len(fat)]
            # Jitter last digits so glyf/cmap isn't a fixed carousel.
            n = n + (rng.randint(0, 97) * ((-1) ** attempt))
            n = max(1000, min(abs(n), 999999))
        elif roll < 0.70:
            # Mid band 4–5 digits random
            n = rng.randint(1000, 99999)
        # else keep generated nlen body (incl. rare 2–3 digit)
        # Ensure all 10 digits appear across war: inject missing digit into amount.
        have = set(str(n))
        miss = [d for d in digs if d not in have]
        if miss and len(str(n)) >= 4:
            s = list(str(n))
            s[-1] = miss[(i + attempt) % len(miss)]
            if s[0] == "0":
                s[0] = "1"
            n = int("".join(s))
    return str(n)


def _stress_phone(rng: random.Random, i: int, attempt: int, *, mobile: bool = False) -> str:
    # 10 digits with rotating coverage of 0–9.
    # T-Bank / bank phone-transfer corpus: DEF must be mobile 9xx
    # (Proton TBANK_PHONE_DEF_NOT_MOBILE).
    digs = list(_DIG)
    rng.shuffle(digs)
    extra = [str((i + attempt + k) % 10) for k in range(10)]
    body = list((digs + extra)[:10])
    if mobile:
        # Keep full digit coverage in the remaining 7; DEF is 9 + two rotating digits.
        d1 = digs[(i + attempt) % len(digs)]
        d2 = digs[(i + attempt + 3) % len(digs)]
        body[0] = "9"
        body[1] = d1
        body[2] = d2
        # Ensure leading never collapses; body[0] already 9.
    body_s = "".join(body)
    return f"+7 ({body_s[:3]}) {body_s[3:6]}-{body_s[6:8]}-{body_s[8:10]}"


def _last4_asc_score(last4: str) -> int:
    """Ascending +1 steps in last4 (Proton TBANK_CARD_LAST4_SEQUENTIAL score≥3)."""
    d = [int(c) for c in last4 if c.isdigit()]
    if len(d) < 2:
        return 0
    return sum(1 for a, b in zip(d, d[1:]) if (b - a) % 10 == 1)


def _stress_card_pan(bin4: str, rng: random.Random, i: int, attempt: int) -> str:
    """16-digit PAN whose last4 is not an ascending ladder."""
    for n in range(64):
        mid = "".join(
            str((i * 17 + attempt * 13 + n * 7 + k * 11 + 3) % 10) for k in range(8)
        )
        last4 = f"{(i * 137 + attempt * 41 + n * 97 + 19) % 10000:04d}"
        # Break residual +1 runs.
        L = list(last4)
        for k in range(1, 4):
            if (int(L[k]) - int(L[k - 1])) % 10 == 1:
                L[k] = str((int(L[k]) + 3 + n) % 10)
        last4 = "".join(L)
        if _last4_asc_score(last4) < 3:
            return (bin4 + mid + last4)[:16]
    # Deterministic non-ladder fallback.
    return (bin4 + "3915827406")[:16]


def _payload(method: str, i: int, attempt: int = 0) -> dict:
    rng = random.Random(
        (41072026 + abs(hash(method)) % 10000 + i * 997 + attempt * 131)
        ^ (int(time.time()) // 3)  # refresh every few seconds — not a fixed name carousel
    )
    corpus_face: dict | None = None
    # War / stress: mash FIO like «Цукенгшщзх Фывапролджэ» — FULL alphabet, no
    # corpus-only / twin-safe trim. Generators must hydrate glyphs; never restrict.
    if method.startswith("tbank"):
        sender = _mash_fio(rng, i, attempt)
        receiver = _mash_fio(rng, i + 3, attempt + 1)
        # Optional amount/phone from corpus for flate-stable digits only — not FIO.
        if method in ("tbank_card_sber", "tbank_card_tbank", "tbank_nocomm"):
            corpus_face = _corpus_tbank_face(i, attempt, method=method)
    elif method.startswith("sber"):
        # Proton exact FIO templates — mash letters, fixed token shape.
        sender = _mash_fio(rng, i, attempt, shape="sber_send")
        receiver = _mash_fio(rng, i + 7, attempt + 1, shape="sber_recv")
    elif method == "alfa_phone":
        sender = _mash_fio(rng, i, attempt, shape="alfa_phone")
        receiver = _mash_fio(rng, i + 5, attempt + 2, shape="alfa_phone")
    elif method.startswith("alfa"):
        sender = _mash_fio(rng, i, attempt)
        receiver = _mash_fio(rng, i + 5, attempt + 2)
    else:
        sender = _mash_fio(rng, i, attempt)
        receiver = _mash_fio(rng, i + 2, attempt)
    # Full alphabet + all digits — no charset quarantine in stress.
    amount = _stress_amount(rng, i, attempt, method=method)
    need_mobile = (
        method.endswith("_phone")
        or method.startswith("sber")  # SBER_PHONE_DEF_NOT_MOBILE on SBP too
        or method.startswith("tbank")  # TBANK_PHONE_DEF_NOT_MOBILE on SBP face too
        or method.startswith("alfa")
    )
    phone = _stress_phone(rng, i, attempt, mobile=need_mobile)
    hh, mm = rng.randint(8, 22), rng.randint(0, 59)
    # Proton TBANK_DATETIME_ZERO_SECONDS — seconds must be 1–59.
    ss = 1 + (rng.randint(0, 58) + i * 7 + attempt) % 59
    # Face date must sit in Proton profile epoch (G1/00117 ≥2026-06-29).
    # April/May → T-TBANK-SBP-PROFILE-EPOCH-EARLY HARD.
    date = f"12.07.2026  {hh:02d}:{mm:02d}:{ss:02d}"
    if method.startswith("alfa"):
        date = f"15.07.2026 {hh:02d}:{mm:02d}:{ss:02d}"
    if method.startswith("sber"):
        date = f"12.07.2026 {hh:02d}:{mm:02d}:{ss:02d}"

    data = {
        "date_time": date,
        "amount": amount,
        "sender": sender,
        "receiver": receiver,
        "phone": phone,
        "receipt_num": "авто",
    }
    if method.startswith("sber"):
        parts = date.split()
        data["date"] = parts[0] if parts else date
        data["time"] = parts[1] if len(parts) > 1 else "12:00:00"
        # Shapes already Proton-exact from _mash_fio(sber_*); do not re-strip.
        data["sender"] = sender
        data["receiver"] = receiver
        data["sender_name"] = sender
        data["receiver_name"] = receiver
        # Short banks only — donor-common full names (no «ВТБ»→«Банк ВТБ» expand).
        _sber_banks = (
            "Сбербанк",
            "Сбербанк",
        )
        data["bank_name"] = _sber_banks[(i + attempt) % len(_sber_banks)] if method == "sber_sbp" else ""
        if method == "sber_sbp":
            data["recipient_bank"] = data["bank_name"]
        # Keep mash FIO short enough for Jasper identity slots (~37–43 B).
        if method == "sber_sbp":
            def _trim_sber_face(name: str, max_chars: int = 18) -> str:
                t = " ".join(str(name or "").split())
                if len(t) <= max_chars:
                    return t
                parts = t.split(" ")
                if len(parts) >= 3:
                    return f"{parts[0][:6]} {parts[1][:8]} {parts[-1]}"[:max_chars]
                return t[:max_chars]
            data["sender"] = _trim_sber_face(data["sender"], 20)
            data["receiver"] = _trim_sber_face(data["receiver"], 18)
            data["sender_name"] = data["sender"]
            data["receiver_name"] = data["receiver"]
    if method == "tbank_card_sber":
        data["recipient_bank"] = "Сбербанк"
        # Card channel has no phone on face — prefer corpus masked PAN (equal-CID).
        data.pop("phone", None)
        if corpus_face and corpus_face.get("card"):
            data["receiver_card"] = corpus_face["card"]
            data["card"] = corpus_face["card"]
        else:
            pan = _stress_card_pan("4276", rng, i, attempt)
            data["receiver_card"] = f"{pan[:6]}******{pan[-4:]}"
            data["card"] = data["receiver_card"]
        # Keep donor face date when present — digit entropy stays flate-stable
        # (changing DD/MM while remixing receipt often misses /Length → dynamic FAKE).
        face_date = (corpus_face or {}).get("date") or ""
        if face_date and re.match(r"\d{2}\.\d{2}\.2026", face_date):
            data["date_time"] = face_date
    if method == "tbank_card_tbank":
        data.pop("phone", None)
        pan = _stress_card_pan("5536", rng, i, attempt)
        data["receiver_card"] = f"*{pan[-4:]}"
        data["card"] = data["receiver_card"]
    if method == "tbank_nocomm":
        data.pop("phone", None)
        # Face has no receiver/bank — only sender + card.
        data.pop("receiver", None)
        pan = _stress_card_pan("2200", rng, i, attempt)
        data["card"] = f"{pan[:6]}******{pan[-4:]}"
        data["receiver_card"] = data["card"]
    if method.startswith("tbank") and corpus_face and corpus_face.get("bank"):
        data["recipient_bank"] = corpus_face["bank"]
        data["bank"] = corpus_face["bank"]
    if method.startswith("alfa"):
        data["bank"] = "Сбербанк"
        data["recipient_bank"] = "Сбербанк"
        # Avoid ascending-digit ladder / period-10 (ALFA_DEBIT_ACCOUNT_* HARD).
        acc_body: list[str] = []
        for k in range(14):
            d = (i * 17 + attempt * 23 + k * 37 + (k * k * 7) + 5) % 10
            if k and (d - int(acc_body[k - 1])) % 10 == 1:
                d = (d + 3) % 10
            if k >= 10 and d == int(acc_body[k - 10]):
                d = (d + 1 + (k % 3)) % 10
            acc_body.append(str(d))
        for k in range(10, 14):
            if all(acc_body[j] == acc_body[j % 10] for j in range(14)):
                acc_body[k] = str((int(acc_body[k]) + 4 + k) % 10)
        data["account"] = "408178" + "".join(acc_body)
        if method == "alfa_card":
            data.pop("sender", None)
            data.pop("receiver", None)
            data.pop("account", None)
            data.pop("bank", None)
            data.pop("recipient_bank", None)
            data.pop("phone", None)
            # MIR BIN 2200xx — Proton ALFA_CARD_BIN_INVALID otherwise
            d1 = _stress_card_pan("2200", rng, i, attempt)
            d2 = _stress_card_pan("2200", rng, i + 11, attempt + 3)
            data["sender_card"] = f"{d1[:6]}******{d1[-4:]}"
            data["receiver_card"] = f"{d2[:6]}******{d2[-4:]}"
        else:
            data["receiver"] = sender
            data.pop("sender", None)
    return data


_LAST_TG_SEND = 0.0  # pace real Proton sends


def _visual_gate(pdf: bytes, payload: dict, method: str) -> Tuple[bool, str]:
    """Exact night gate: every user field is HARD before Proton."""
    try:
        from emit_quality_gate import visual_integrity

        vok, vwhy = visual_integrity(
            pdf, expect=payload, bank_hint=method, strict_fio=True,
        )
    except Exception as exc:
        return False, f"visual-import:{exc}"
    if vok:
        return True, "ok"
    w = (vwhy or "").lower()
    hard = (
        "tofu",
        "too-few-blocks",
        "visual-open",
        "visual-import",
        "missing-amt",
        "missing-date",
        "wrong-day",
        "missing-year",
        "missing-phone",
        "empty-glyph",
        "missing-bank",
        "missing-field",
        "donor-fio",
        "left-clip",
        "right-overflow",
        "lead-pad",
        "not-right",
        "not-left",
        "date-center",
        "amt-edge",
        "odd-tj",
        "ruble-gap",
        "pua-onpage",
        "amt-mismatch",
        "stamp-junk",
    )
    if any(h in w for h in hard):
        return False, vwhy
    # Soft-cover remaps rare letters (Ж→З) — face ≠ payload FIO is advisory.
    # Still send to Proton so war can learn real HARD flags.
    return True, f"soft:{vwhy}"


async def _pace_tg(cycle: float) -> None:
    """At most 4 checks/min → cycle≥15s between actual TG sends."""
    global _LAST_TG_SEND
    gap = max(15.0, float(cycle))
    now = time.monotonic()
    wait = gap - (now - _LAST_TG_SEND)
    if _LAST_TG_SEND > 0 and wait > 0:
        await asyncio.sleep(wait)
    _LAST_TG_SEND = time.monotonic()


def _glyph_fail(why: str) -> bool:
    w = (why or "").lower()
    # Soft-cover lookalike misses are not glyph-library gaps — don't bake shells.
    if "missing-first" in w or "missing-last" in w or "missing-recv" in w:
        if "tofu" not in w and "empty-glyph" not in w:
            return False
    return any(
        x in w
        for x in (
            "tofu",
            "glyph",
            "missing-first",
            "missing-last",
            "missing-recv",
            "visual-fio",
            "gen_none",
        )
    )


def _try_bake_shell(method: str, reason: str) -> bool:
    """Offline expand full-charset shell; deploy if bake ok. Cooldown 10 min/channel.

    NEVER bake on mosaic/glyph-outline FAKEs — baking grafts into unlocked
    shells poisons trusted Jasper FontFile2 → more MOSAIC HARD.
    """
    # NEVER bake tbank_phone — unlocked Medium is K-FONT-002 + F2 size outlier.
    if method.startswith("tbank_phone"):
        _log(f"{method} BAKE_SHELL skip (phone lean-only) reason={reason}")
        return False
    r = (reason or "").lower()
    if any(
        k in r
        for k in (
            "mosaic",
            "glyph",
            "font|glyph",
            "outline",
            "atlas",
            "structure_font",
            # fake_fp:font / FONTFILE2_SIZE — baking fat unlocked worsens HARD.
            "font",
            "fontfile",
            "outlier",
        )
    ):
        _log(f"{method} BAKE_SHELL skip (mosaic-safe) reason={reason}")
        return False
    now = time.time()
    last = _BAKE_COOLDOWN.get(method, 0)
    if now - last < 600:
        _log(f"{method} bake skip cooldown ({int(600 - (now - last))}s) reason={reason}")
        return False
    _BAKE_COOLDOWN[method] = now
    _log(f"{method} BAKE_SHELL start reason={reason}")
    try:
        sys.path.insert(0, str(_DIR))
        from shell_bake import bake_channel, deploy_changed

        ok = bake_channel(method)
        if not ok:
            _log(f"{method} BAKE_SHELL fail")
            return False
        # Deploy generator-facing templates
        files = []
        if method.startswith("tbank_phone"):
            files = ["templates/T_phone_unlocked.pdf", "tbank_phone_stealth.py"]
        elif method.startswith("tbank_sbp"):
            files = ["templates/T_sbp_unlocked.pdf", "tbank_sbp_stealth.py"]
        elif method.startswith("sber"):
            files = [
                "templates/S_sbp_unlocked.pdf",
                "templates/S_sbp_runtime.pdf",
                "sber_dynamic.py",
                "sber_glyph_library.py",
            ]
        elif method.startswith("alfa"):
            kind = "phone" if "phone" in method else ("card" if "card" in method else "sbp")
            files = [f"templates/Alfa_{kind}_unlocked.pdf"]
        deploy_changed(files or None)
        st = {}
        if _CHARSET_STATUS.is_file():
            try:
                st = json.loads(_CHARSET_STATUS.read_text(encoding="utf-8"))
            except Exception:
                st = {}
        st[method] = {
            "baked_at": datetime.now().isoformat(),
            "reason": reason,
        }
        _CHARSET_STATUS.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        _log(f"{method} BAKE_SHELL ok + deploy")
        return True
    except Exception as exc:
        _log(f"{method} BAKE_SHELL exc {exc}")
        return False


def _fake_fingerprint(raw: str) -> str:
    low = (raw or "").lower()
    keys = []
    for k in (
        "structure",
        "структур",
        "font",
        "glyph",
        "length",
        "xref",
        "metadata",
        "id",
        "sbp",
        "padding",
        "bbox",
        "trailer",
        "flate",
        "blacklist",
        "черн",
        "сер",
        "бел",
        "edge",
        "выравн",
        "смещен",
    ):
        if k in low:
            keys.append(k)
    return "|".join(keys[:6]) or "unknown"


def _classify_fake(fp: str) -> str:
    """Map fingerprint → fix class for hooks."""
    if any(x in fp for x in ("font", "glyph", "structure", "структур", "length", "bbox", "flate", "xref")):
        return "structure_font"
    if any(x in fp for x in ("edge", "выравн", "смещен", "padding")):
        return "edge"
    if any(x in fp for x in ("id", "metadata", "trailer")):
        return "id_meta"
    if any(x in fp for x in ("черн", "сер", "бел", "blacklist")):
        return "blacklist"
    return "other"


def _load_json(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _quarantine_method(method: str, fp: str, cls: str, sample: str) -> None:
    data = _load_json(_QUARANTINE)
    data[method] = {
        "fp": fp,
        "class": cls,
        "at": datetime.now().isoformat(),
        "sample": (sample or "")[:400],
        "active": True,
    }
    _QUARANTINE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"{method} QUARANTINE class={cls} fp={fp}")


def _is_quarantined(method: str) -> bool:
    data = _load_json(_QUARANTINE)
    row = data.get(method) or {}
    return bool(row.get("active"))


def _note_pass(method: str) -> None:
    n = int(_PASS_STREAK.get(method, 0)) + 1
    _PASS_STREAK[method] = n
    st = _load_json(_CHARSET_STATUS)
    row = st.setdefault(method, {})
    row["pass_streak"] = n
    row["last_pass"] = datetime.now().isoformat()
    if n >= 5:
        row["charset_ok"] = True
        row["charset_ok_at"] = datetime.now().isoformat()
        _log(f"{method} charset_ok (PASS streak={n})")
    _CHARSET_STATUS.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _note_fake(method: str, raw: str) -> None:
    fp = _fake_fingerprint(raw)
    cls = _classify_fake(fp)
    _PASS_STREAK[method] = 0
    data = _load_json(_FAKE_FP)
    bucket = data.setdefault(method, {})
    row = bucket.setdefault(fp, {"n": 0, "last": "", "sample": "", "class": cls})
    row["n"] = int(row.get("n") or 0) + 1
    row["last"] = datetime.now().isoformat()
    row["sample"] = (raw or "")[:400]
    row["class"] = cls
    _FAKE_FP.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if row["n"] >= 3:
        _log(f"{method} FAKE_FP x{row['n']}: {fp} class={cls}")
        if cls == "structure_font":
            _try_bake_shell(method, f"fake_fp:{fp}")
        elif cls in ("edge", "id_meta", "blacklist") and row["n"] >= _FAKE_QUARANTINE_N:
            _quarantine_method(method, fp, cls, raw)
        elif row["n"] >= _FAKE_QUARANTINE_N:
            _quarantine_method(method, fp, cls, raw)


async def _send_one(client, cfg, path: Path, caption: str) -> Tuple[str, str]:
    bot = _norm_bot(cfg.get("TG_PROTON_BOT", DEFAULT_PROTON), DEFAULT_PROTON)
    msgs = await client.get_messages(bot, limit=1)
    after = msgs[0].id if msgs else 0
    await client.send_file(bot, str(path), caption=caption[:900])
    v = await _wait_verdict(client, bot, after, cfg, parser=_parse_proton)
    raw = ""
    try:
        async for msg in client.iter_messages(bot, limit=6):
            if msg.id <= after:
                continue
            t = (msg.text or "").strip()
            if not t:
                continue
            low = t.lower()
            if any(x in low for x in ("оригинал", "фейк", "чисто", "verdict", "подделк")):
                raw = t[:1600]
                break
    except Exception:
        pass
    return v, raw


async def run_method(method: str, *, n: int, cycle: float, war: bool = False) -> dict:
    gens = _load_gens()
    gen = gens[method]
    mdir = _OUT / method
    mdir.mkdir(parents=True, exist_ok=True)
    stats: Dict[str, Any] = {
        "method": method,
        "gen_ok": 0,
        "gen_fail": 0,
        "visual_fail": 0,
        "tg_pass": 0,
        "tg_fake": 0,
        "tg_other": 0,
        "samples": [],
    }
    _log(
        f"===== METHOD START: {method} (n={n}, ~{60/max(cycle,1):.1f}/min"
        f"{', WAR' if war else ''}) ====="
    )
    client, cfg = await open_onlypdf_client()
    # Fast fail — don't sit long on flood TIMEOUT
    cfg["TG_WAIT_SECONDS"] = "12"
    cfg["TG_POLL_SECONDS"] = "0.35"
    try:
        sent = 0
        attempt_budget = n * 8 if war else n * 8
        ai = 0
        gen_none_streak = 0
        while sent < n and ai < attempt_budget:
            if _STOP.exists() and not war:
                _log("STOP mid-method")
                break
            if war and _STOP.exists():
                try:
                    _STOP.unlink()
                except Exception:
                    pass
            ai += 1
            t0 = time.monotonic()
            payload = _payload(method, sent + int(time.time()) % 700, ai)
            try:
                pdf = gen(payload)
            except Exception as exc:
                stats["gen_fail"] += 1
                gen_none_streak += 1
                _log(f"{method} GEN_EXC {exc}")
                if war and gen_none_streak >= 12:
                    _log(f"{method} GEN_NONE streak={gen_none_streak} — skip method")
                    break
                continue
            if not pdf:
                stats["gen_fail"] += 1
                gen_none_streak += 1
                _log(f"{method} GEN_NONE {payload.get('sender')}")
                # Never bake on GEN_NONE — flate/slot misses aren't charset gaps;
                # sber bake overwrites fixed S_sbp_runtime and re-poisons labels.
                if war and gen_none_streak >= 12:
                    _log(f"{method} GEN_NONE streak={gen_none_streak} — skip method")
                    break
                continue
            gen_none_streak = 0
            vok, vwhy = _visual_gate(pdf, payload, method)
            if not vok:
                stats["visual_fail"] += 1
                _log(f"{method} VISUAL_REJECT {vwhy} | {payload.get('sender')} amt={payload.get('amount')}")
                if _glyph_fail(vwhy):
                    _try_bake_shell(method, vwhy)
                await asyncio.sleep(0.15)
                continue
            stats["gen_ok"] += 1
            sent += 1
            path = mdir / f"seq_{int(time.time())}_{sent:02d}.pdf"
            path.write_bytes(pdf)
            caption = (
                f"SEQ {method} #{sent}/{n}\n"
                f"{payload.get('sender')} → {payload.get('receiver')}\n"
                f"amt={payload.get('amount')} phone={payload.get('phone')}"
            )
            try:
                await _pace_tg(cycle)
                tv, raw = await _send_one(client, cfg, path, caption)
            except Exception as exc:
                stats["tg_other"] += 1
                _log(f"{method} TG_ERR {type(exc).__name__}:{exc}")
                await asyncio.sleep(1.5)
                sent -= 1  # retry slot
                stats["gen_ok"] -= 1
                continue

            sec = round(time.monotonic() - t0, 1)
            row = {
                "ts": datetime.now().isoformat(),
                "method": method,
                "tg": tv,
                "payload": payload,
                "path": str(path),
                "raw": raw[:900],
                "sec": sec,
            }
            _append("queue_tg.jsonl", row)
            stats["samples"].append({"tg": tv, "sec": sec, "file": path.name})
            if tv == "PASS":
                stats["tg_pass"] += 1
                _log(f"{method} #{sent} TG=PASS ({sec}s)")
                _note_pass(method)
            elif tv == "FAKE":
                stats["tg_fake"] += 1
                _append("queue_fake.jsonl", row)
                _log(f"{method} #{sent} TG=FAKE ({sec}s)")
                if raw:
                    _log(f"{method} EXPLAIN: {raw[:450].replace(chr(10), ' | ')}")
                _note_fake(method, raw)
                stop_payload = {
                    "method": method,
                    "at": datetime.now().isoformat(),
                    "file": path.name,
                    "explain": (raw or "")[:1200],
                    "payload": payload,
                }
                _STOP_FAKE.write_text(
                    json.dumps(stop_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                if war:
                    _log(f"WAR_FAKE {method} — logged, continue firing")
                else:
                    _STOP.write_text(
                        f"FAKE {method} — fix+deploy then --force\n",
                        encoding="utf-8",
                    )
                    _log(f"STOP_ON_FAKE {method} — next check blocked until fix+deploy")
                    return stats
            else:
                stats["tg_other"] += 1
                _log(f"{method} #{sent} TG={tv} ({sec}s)")

            # Spacing handled by _pace_tg (≤4/min). Brief pause only after TIMEOUT.
            if tv == "TIMEOUT":
                await asyncio.sleep(0.8)
        if sent < n and not war and not _STOP.exists():
            stop_payload = {
                "method": method,
                "at": datetime.now().isoformat(),
                "reason": "exact_generation_exhausted",
                "sent": sent,
                "target": n,
                "gen_fail": stats["gen_fail"],
                "visual_fail": stats["visual_fail"],
            }
            (_OUT / "STOP_GENERATION.json").write_text(
                json.dumps(stop_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _STOP.write_text(
                f"EXACT_GENERATION_FAILED {method} — fix+deploy then --force\n",
                encoding="utf-8",
            )
            _log(
                f"STOP_ON_GENERATION {method} — exact PDF {sent}/{n}; "
                "fix before next method"
            )
        elif sent < n and war:
            _log(
                f"WAR_GEN_PARTIAL {method} sent={sent}/{n} "
                f"gen_fail={stats['gen_fail']} — continue round"
            )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    _log(
        f"===== METHOD DONE: {method} pass={stats['tg_pass']} fake={stats['tg_fake']} "
        f"other={stats['tg_other']} gen_fail={stats['gen_fail']} "
        f"visual_fail={stats['visual_fail']} ====="
    )
    (_OUT / f"method_{method}.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return stats


async def run_queue(
    *,
    n: int = 3,
    cycle: float = 8.0,
    start: str = "",
    only: str = "",
    forever: bool = True,
    war: bool = False,
) -> int:
    if only:
        methods = [m.strip() for m in only.split(",") if m.strip()]
    else:
        methods = list(QUEUE)
        if start and start in methods:
            methods = methods[methods.index(start) :]

    round_i = 0
    while True:
        if _STOP.exists() and not war:
            _log("STOP — exit queue")
            return 0
        if war and _STOP.exists():
            try:
                _STOP.unlink()
            except Exception:
                pass
        round_i += 1
        _log(f"######## QUEUE ROUND {round_i} (~{60/max(cycle,1):.1f} checks/min) ########")
        for idx, method in enumerate(methods):
            if _STOP.exists() and not war:
                return 0
            if _is_quarantined(method) and not war:
                _log(f"SKIP quarantined {method}")
                _save_pos(idx, method, f"round={round_i} quarantined")
                continue
            _save_pos(idx, method, f"round={round_i}")
            try:
                await run_method(method, n=n, cycle=cycle, war=war)
            except Exception:
                _log(f"METHOD_CRASH {method}\n" + traceback.format_exc())
            await asyncio.sleep(0.4)
        if not forever:
            return 0
        _log("Queue complete — restart")
        await asyncio.sleep(1.0)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3)
    ap.add_argument(
        "--cycle",
        type=float,
        default=15.0,
        help="min sec between TG sends (15 = 4/min max)",
    )
    ap.add_argument("--start", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--once", action="store_true")
    ap.add_argument(
        "--force",
        action="store_true",
        help="clear STOP and start (only after fix+deploy)",
    )
    ap.add_argument(
        "--war",
        action="store_true",
        help="endless fire: no STOP on FAKE/gen-fail, auto-clear STOP",
    )
    args = ap.parse_args()
    _OUT.mkdir(parents=True, exist_ok=True)
    # Never auto-clear STOP — agent removes it only after fix+deploy.
    if _STOP.exists() and not args.force and not args.war:
        msg = _STOP.read_text(encoding="utf-8", errors="replace")[:300]
        _log(f"STOP present — refuse start ({msg!r}). Pass --force after fix.")
        return 2
    if (args.force or args.war) and _STOP.exists():
        _STOP.unlink()
        if _STOP_FAKE.exists() and args.force:
            _STOP_FAKE.unlink()
        _log("STOP cleared (--force/--war)")
    if args.force and _QUARANTINE.exists():
        data = _load_json(_QUARANTINE)
        changed = False
        for row in data.values():
            if isinstance(row, dict) and row.get("active"):
                row["active"] = False
                row["cleared_by"] = "force"
                changed = True
        if changed:
            _QUARANTINE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _log("QUARANTINE cleared (--force)")
    cycle = max(15.0, float(args.cycle))  # hard cap: never faster than 4/min
    _log(
        f"SEQ QUEUE paced {60/cycle:.1f}/min (cycle={cycle}s) "
        f"stop_on_fake={0 if args.war else 1} war={int(args.war)}"
    )
    return asyncio.run(
        run_queue(
            n=args.n,
            cycle=cycle,
            start=args.start,
            only=args.only,
            forever=not args.once,
            war=bool(args.war),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
