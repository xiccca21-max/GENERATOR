# -*- coding: utf-8 -*-
"""Shared exact-payload fixtures for generator and pre-send gate tests."""
from __future__ import annotations

from typing import Dict, Iterator, Tuple

CYRILLIC = (
    "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЫЬЭЮЯ"
    "абвгдежзиклмнопрстуфхцчшщыьэюя"
)
LATIN = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
DIGITS = "0123456789"
FIELD_PUNCTUATION = " .,-+()*/№:@_"
FULL_FIELD_CHARSET = CYRILLIC + LATIN + DIGITS + FIELD_PUNCTUATION


def iter_charset_chunks(width: int = 18) -> Iterator[str]:
    """Yield every required character in slot-sized deterministic chunks."""
    if width < 1:
        raise ValueError("width must be positive")
    for start in range(0, len(FULL_FIELD_CHARSET), width):
        yield FULL_FIELD_CHARSET[start : start + width]


def stress_payloads(chunk: str) -> Dict[str, Dict[str, str]]:
    """Return method-shaped payloads without auto/sentinel values."""
    fio = f"{chunk} Я."
    common = {
        "amount": "75670",
        "date_time": "10.07.2026 12:34:56",
    }
    return {
        "tbank_sbp": {
            **common, "sender": fio, "receiver": f"{chunk} А.",
            "phone": "+7 (916) 123-45-67", "bank": chunk,
            "account": "40817810123456789012",
            "sbp_id": "A61551545348731O0G10080011770901",
            "receipt_num": "1-123-456-789-012",
        },
        "tbank_phone": {
            **common, "sender": fio, "receiver": f"{chunk} Б.",
            "phone": "+7 (916) 123-45-67",
            "receipt_num": "1-123-456-789-012",
        },
        "tbank_card_sber": {
            **common, "sender": fio, "receiver": f"{chunk} В.",
            "bank": "Сбербанк", "receiver_card": "2202201234560942",
            "receipt_num": "1-123-456-789-012",
        },
        "tbank_card_tbank": {
            **common, "sender": fio, "receiver": f"{chunk} Г.",
            "receiver_card": "2202201234567015",
            "receipt_num": "1-123-456-789-012",
        },
        "tbank_nocomm": {
            **common, "sender": fio, "receiver_card": "2204241234561891",
            "receipt_num": "1-123-456-789-012",
        },
        "sber_sbp": {
            **common, "sender": fio, "receiver": f"{chunk} Д.",
            "phone": "+7 (916) 123-45-67", "bank": "Т-Банк",
            "account": "40817810123456789012",
            "sbp_id": "A61551545348731O0G10080011770901",
        },
        "sber_phone": {
            **common, "sender": fio, "receiver": f"{chunk} Е.",
            "phone": "+7 (916) 123-45-67", "account": "40817810123456789012",
            "operation_num": "1000000005312075311",
        },
        "alfa_sbp": {
            **common, "receiver": fio, "phone": "+7 (916) 123-45-67",
            "bank": "Сбербанк", "account": "40817810123456789012",
            "operation_num": "C160406261736948",
            "sbp_id": "A61551545348731O0G10080011770901",
            "message": chunk,
        },
        "alfa_card": {
            **common, "sender_card": "2200151234568946",
            "receiver_card": "2202201234568275",
            "operation_num": "Z093103250153021",
        },
        "alfa_phone": {
            **common, "receiver": fio, "phone": "+7 (916) 123-45-67",
            "account": "40817810123456789012",
            "operation_num": "C071504260544819", "message": chunk,
        },
    }


def iter_full_charset_payloads(width: int = 18) -> Iterator[Tuple[str, str, Dict[str, str]]]:
    """Yield ``(method, chunk, payload)`` covering the full supported charset."""
    for chunk in iter_charset_chunks(width):
        for method, payload in stress_payloads(chunk).items():
            yield method, chunk, payload
