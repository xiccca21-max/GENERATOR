# -*- coding: utf-8 -*-
"""Canonical pre-send profiles for the ten live receipt channels.

The profile is intentionally data-only.  Generators may not override it.  The
first reference is the shipped donor; corpus roots document where the measured
baseline came from.  ``effective_size_band`` narrows the declared band around
the actual donor size when the donor is available.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
ORIGINALS = Path(r"C:\Users\fanis\OneDrive\Desktop\чеки")


@dataclass(frozen=True)
class FieldContract:
    kind: str = "text"
    required: bool = False
    display_mask: Optional[str] = None
    anchor: Optional[str] = None
    repeats: int = 1


@dataclass(frozen=True)
class MethodProfile:
    method: str
    bank: str
    references: Tuple[Path, ...]
    corpus_roots: Tuple[Path, ...]
    size_band: Tuple[int, int]
    size_tolerance: int
    anchors: Mapping[str, Tuple[str, float, float]]
    fields: Mapping[str, FieldContract]
    required_markers: Tuple[str, ...]
    structure_hook: Optional[str] = None
    keywords_by_face_date: bool = False
    allow_visible_pua: FrozenSet[str] = frozenset()
    amount_ruble_gap: Optional[Tuple[float, float]] = None


def _f(
    kind: str = "text",
    *,
    required: bool = False,
    display_mask: Optional[str] = None,
    anchor: Optional[str] = None,
    repeats: int = 1,
) -> FieldContract:
    return FieldContract(kind, required, display_mask, anchor, repeats)


TBANK_ANCHORS = {
    "date": ("left", 20.0, 2.5),
    "value": ("right", 250.0, 3.5),
    "amount": ("right_before_currency", 243.68, 8.0),
    "total": ("right_before_currency", 237.77, 8.0),
}
SBER_ANCHORS = {
    "value": ("left", 21.0, 4.0),
    "date": ("center", 153.0, 18.0),
}
ALFA_ANCHORS = {
    "left": ("left", 35.45, 8.0),
    "right": ("left", 304.75, 12.0),
    "date": ("left", 35.45, 8.0),
    "header_date": ("right", 559.69, 12.0),
}
ALFA_STATEMENT_ANCHORS = {
    "left": ("left", 146.65, 8.0),
    "right": ("right", 567.0, 10.0),
    "date": ("left", 146.65, 8.0),
}

TBANK_COMMON = {
    "amount": _f("amount", required=True, anchor="amount", repeats=2),
    "date_time": _f("datetime", required=True, anchor="date"),
    "sender": _f("fio", required=True, anchor="value"),
    "receipt_num": _f("id"),
    "commission": _f("amount"),
}
SBER_COMMON = {
    "amount": _f("amount", required=True, anchor="value"),
    "date_time": _f("datetime", required=True, anchor="date"),
    "sender": _f("fio", required=True, anchor="value"),
    "receiver": _f("fio", required=True, anchor="value"),
    "phone": _f("phone", required=True, anchor="value"),
    "commission": _f("amount"),
}
ALFA_COMMON = {
    "amount": _f("amount", required=True, anchor="left"),
    "date_time": _f("datetime", required=True, anchor="date"),
    "operation_num": _f("id", anchor="left"),
    "commission": _f("amount", anchor="left"),
}


METHOD_PROFILES: Dict[str, MethodProfile] = {
    "tbank_sbp": MethodProfile(
        "tbank_sbp", "tbank",
        (ROOT / "templates" / "T_sbp_original.pdf",),
        (ORIGINALS / "т банк",),
        (58_000, 63_000), 1_800, TBANK_ANCHORS,
        {
            **TBANK_COMMON,
            "receiver": _f("fio", required=True, anchor="value"),
            "phone": _f("phone", required=True, anchor="value"),
            "bank": _f("text", required=True, anchor="value"),
            "account": _f("account", display_mask="tbank_account", anchor="value"),
            "sbp_id": _f("id", anchor="value"),
        },
        ("Перевод", "Сумма", "Итого", "СБП"),
        "tbank_sbp", True, amount_ruble_gap=(-1.0, 6.0),
    ),
    "tbank_phone": MethodProfile(
        "tbank_phone", "tbank",
        (ROOT / "templates" / "T_phone_original.pdf",),
        (ORIGINALS / "т банк",),
        (57_000, 63_000), 1_800, TBANK_ANCHORS,
        {
            **TBANK_COMMON,
            "receiver": _f("fio", required=True, anchor="value"),
            "phone": _f("phone", required=True, anchor="value"),
        },
        ("Перевод", "По номеру телефона", "Сумма", "Итого"),
        "tbank_phone", True, amount_ruble_gap=(-1.0, 6.0),
    ),
    "tbank_card_sber": MethodProfile(
        "tbank_card_sber", "tbank",
        (ROOT / "templates" / "T_card_sber_original.pdf", ROOT / "templates" / "T_original.pdf"),
        (ORIGINALS / "т банк",),
        (57_000, 63_000), 1_800, TBANK_ANCHORS,
        {
            **TBANK_COMMON,
            "receiver": _f("fio", required=True, anchor="value"),
            "bank": _f("text", required=True, anchor="value"),
            "receiver_card": _f("card", required=True, display_mask="card_6_6_4", anchor="value"),
        },
        ("Перевод", "По номеру карты", "Сумма", "Итого"),
        "tbank_card_sber", True, amount_ruble_gap=(-1.0, 6.0),
    ),
    "tbank_card_tbank": MethodProfile(
        "tbank_card_tbank", "tbank",
        (ROOT / "templates" / "T_card_tbank_original.pdf",),
        (ORIGINALS / "т банк",),
        (57_000, 63_000), 1_800, TBANK_ANCHORS,
        {
            **TBANK_COMMON,
            "receiver": _f("fio", required=True, anchor="value"),
            "receiver_card": _f("card", required=True, display_mask="card_last4", anchor="value"),
        },
        ("Перевод", "Клиенту Т-Банка", "Сумма", "Итого"),
        "tbank_card_tbank", True, amount_ruble_gap=(-1.0, 6.0),
    ),
    "tbank_nocomm": MethodProfile(
        "tbank_nocomm", "tbank",
        (ROOT / "templates" / "T_nocomm_original.pdf",),
        (ORIGINALS / "т банк",),
        (57_000, 63_000), 1_800, TBANK_ANCHORS,
        {
            **TBANK_COMMON,
            "receiver_card": _f("card", required=True, display_mask="card_6_6_4", anchor="value"),
        },
        ("Перевод", "На карту", "Сумма", "Итого"),
        "tbank_nocomm", True, amount_ruble_gap=(-1.0, 6.0),
    ),
    "sber_sbp": MethodProfile(
        "sber_sbp", "sber",
        (ROOT / "templates" / "S_sbp_original.pdf",),
        (ORIGINALS / "сбер",),
        (100_000, 104_999), 2_200, SBER_ANCHORS,
        {
            **SBER_COMMON,
            "bank": _f("text", required=True, anchor="value"),
            "account": _f("account", display_mask="last4", anchor="value"),
            "sbp_id": _f("id", anchor="value"),
        },
        ("Чек по операции", "Перевод по СБП", "Сумма перевода"),
        "sber_sbp",
    ),
    "sber_phone": MethodProfile(
        "sber_phone", "sber",
        (ROOT / "templates" / "sber_shells" / "сбер по номеру телефона на сбер.pdf",),
        (ORIGINALS / "сбер",),
        (42_000, 46_000), 1_500, SBER_ANCHORS,
        {
            **SBER_COMMON,
            "account": _f("account", display_mask="last4", anchor="value"),
            "operation_num": _f("id", anchor="value"),
        },
        ("Чек по операции", "Перевод клиенту СберБанка", "Сумма перевода"),
        "sber_phone",
    ),
    "alfa_sbp": MethodProfile(
        "alfa_sbp", "alfa",
        (ROOT / "templates" / "Alfa_sbp_original.pdf",),
        (ORIGINALS / "альфа",),
        (58_000, 59_087), 1_000, ALFA_ANCHORS,
        {
            **ALFA_COMMON,
            "receiver": _f("fio", required=True, anchor="left"),
            "phone": _f("phone", required=True, anchor="right"),
            "bank": _f("text", required=True, anchor="right"),
            "account": _f("account", required=True, anchor="right"),
            "sbp_id": _f("id", anchor="right"),
            "message": _f("text", required=True, anchor="right"),
        },
        ("Квитанция о переводе по СБП", "Сумма перевода", "Дата и время перевода"),
        "alfa_sbp",
    ),
    "alfa_card": MethodProfile(
        "alfa_card", "alfa",
        (ROOT / "templates" / "Alfa_card_original.pdf",),
        (ORIGINALS / "альфа",),
        (58_000, 59_087), 1_000, ALFA_ANCHORS,
        {
            **ALFA_COMMON,
            "sender_card": _f("card", required=True, display_mask="card_6_6_4", anchor="left"),
            "receiver_card": _f("card", required=True, display_mask="card_6_6_4", anchor="right"),
        },
        ("Квитанция о переводе с карты на карту", "Сумма перевода"),
        "alfa_card",
    ),
    "alfa_phone": MethodProfile(
        "alfa_phone", "alfa",
        (ROOT / "templates" / "Alfa_phone_original.pdf",),
        (ORIGINALS / "альфа",),
        # Quartz phone originals ~69.7–71.6 KB (not Oracle SBP ~58–60 KB).
        (69_000, 72_000), 1_000, ALFA_ANCHORS,
        {
            **ALFA_COMMON,
            "receiver": _f("fio", required=True, display_mask="alfa_fio", anchor="left"),
            "phone": _f("phone", required=True, display_mask="alfa_phone", anchor="right"),
            "account": _f("account", required=True, display_mask="alfa_account", anchor="right"),
            "message": _f("text", required=True, anchor="right"),
        },
        ("Квитанция о переводе клиенту Альфа-Банка", "Сумма перевода"),
        "alfa_phone",
    ),
    "alfa_statement": MethodProfile(
        "alfa_statement", "alfa",
        (ROOT / "templates" / "Alfa_statement_original.pdf",),
        (ORIGINALS / "альфа",),
        (90_000, 130_000), 25_000, ALFA_STATEMENT_ANCHORS,
        {
            "amount": _f("amount", required=True, anchor="right", repeats=2),
            "date_time": _f("datetime", required=True, anchor="date"),
            "receiver": _f("fio", required=True, anchor="left"),
            "account": _f("account", required=True, anchor="left"),
        },
        ("Выписка по счету", "Операции по счету", "Код операции"),
        "alfa_statement",
    ),
}


ALIASES = {
    "create_tbank_sbp_stealth": "tbank_sbp",
    "create_tbank_phone_stealth": "tbank_phone",
    "create_tbank_stealth": "tbank_card_sber",
    "create_tbank_card_tbank_stealth": "tbank_card_tbank",
    "create_tbank_nocomm_stealth": "tbank_nocomm",
    "create_sber_sbp_stealth": "sber_sbp",
    "create_sber_phone_stealth": "sber_phone",
    "create_alfa_sbp_stealth": "alfa_sbp",
    "create_alfa_card_stealth": "alfa_card",
    "create_alfa_phone_stealth": "alfa_phone",
    "create_alfa_statement_stealth": "alfa_statement",
}


def get_profile(method: str) -> Optional[MethodProfile]:
    key = (method or "").strip().lower()
    key = ALIASES.get(key, key)
    return METHOD_PROFILES.get(key)


def effective_size_band(profile: MethodProfile) -> Tuple[int, int]:
    """Return declared band intersected with donor-size tolerance when possible."""
    sizes = []
    for path in profile.references:
        try:
            if path.is_file():
                sizes.append(path.stat().st_size)
        except OSError:
            continue
    if not sizes:
        return profile.size_band
    measured = (
        min(sizes) - profile.size_tolerance,
        max(sizes) + profile.size_tolerance,
    )
    lo = max(profile.size_band[0], measured[0])
    hi = min(profile.size_band[1], measured[1])
    return (lo, hi) if lo <= hi else profile.size_band
