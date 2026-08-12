# -*- coding: utf-8 -*-
"""Emit regressions: short + long FIO must not return None (bot «не собралось»).

Live OnlyPDF channels only. Skips blocked (sber_card).
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_TOOLS = os.path.join(ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from onlypdf_safe_names import STRESS_LONG_RECV, STRESS_LONG_SENDERS  # noqa: E402


def _long_sender(i: int = 0) -> str:
    fn, ln = STRESS_LONG_SENDERS[i % len(STRESS_LONG_SENDERS)]
    return f"{fn} {ln}"


class EmitLengthStressTests(unittest.TestCase):
    """create_* must emit PDF for short and long face FIO."""

    def _assert_pdf(self, pdf, label: str) -> None:
        self.assertIsNotNone(pdf, msg=f"emit None: {label}")
        assert pdf is not None
        self.assertTrue(pdf.startswith(b"%PDF-"), msg=label)
        self.assertGreater(len(pdf), 40_000, msg=label)

    def test_tbank_phone_short_and_long(self) -> None:
        from tbank_phone_stealth import create_tbank_phone_stealth

        cases = [
            {
                "date_time": "01.08.2026 19:30:45",
                "amount": "27000",
                "sender": "Дамир Сеничев",
                "receiver": "Тимур М.",
                "phone": "+7 (960) 565-36-73",
                "receipt_num": "1-117-389-080-984",
            },
            {
                "date_time": "01.08.2026 19:30:45",
                "amount": "1500",
                "sender": "Иван Петров",
                "receiver": "Ян К.",
                "phone": "+7 (999) 111-22-33",
                "receipt_num": "авто",
            },
            {
                "date_time": "01.08.2026 19:30:45",
                "amount": "5000",
                "sender": _long_sender(0),
                "receiver": STRESS_LONG_RECV[0],
                "phone": "+7 (999) 222-33-44",
                "receipt_num": "авто",
            },
            {
                "date_time": "01.08.2026 19:30:45",
                "amount": "12345",
                "sender": _long_sender(6),
                "receiver": STRESS_LONG_RECV[6],
                "phone": "+7 (964) 607-43-82",
                "receipt_num": "авто",
            },
        ]
        for i, payload in enumerate(cases):
            with self.subTest(i=i, sender=payload["sender"][:32]):
                self._assert_pdf(create_tbank_phone_stealth(payload), str(payload))

    def test_tbank_sbp_short_and_long(self) -> None:
        from tbank_sbp_stealth import create_tbank_sbp_stealth

        for label, sender, recv in (
            ("short", "Иван Петров", "Анна А."),
            ("long", _long_sender(1), STRESS_LONG_RECV[1]),
        ):
            with self.subTest(label=label):
                pdf = create_tbank_sbp_stealth(
                    {
                        "date_time": "01.08.2026 12:00:00",
                        "amount": "1230",
                        "sender": sender,
                        "recipient": recv,
                        "phone": "+7 (965) 585-66-55",
                        "recipient_bank": "Сбербанк",
                        "receipt_num": "авто",
                    }
                )
                self._assert_pdf(pdf, f"tbank_sbp {label}")

    def test_tbank_card_channels_short_and_long(self) -> None:
        from tbank_stealth_v3 import create_tbank_stealth
        from tbank_card_tbank_stealth import create_tbank_card_tbank_stealth
        from tbank_nocomm_stealth import create_tbank_nocomm_stealth

        for name, fn, extra in (
            ("card_sber", create_tbank_stealth, {"card": "220000******1234", "recipient_bank": "Сбербанк"}),
            ("card_tbank", create_tbank_card_tbank_stealth, {"card": "*1234"}),
            ("nocomm", create_tbank_nocomm_stealth, {"card": "220000******1234"}),
        ):
            for label, sender, recv in (
                ("short", "Иван Петров", "Анна А."),
                ("long", _long_sender(2), STRESS_LONG_RECV[2]),
            ):
                with self.subTest(ch=name, label=label):
                    data = {
                        "date_time": "01.08.2026 12:00:00",
                        "amount": "5000",
                        "sender": sender,
                        "receiver": recv,
                        "receipt_num": "авто",
                        **extra,
                    }
                    self._assert_pdf(fn(data), f"{name} {label}")

    def test_alfa_sbp_phone_short_and_long(self) -> None:
        from alfa_sbp_stealth import create_alfa_sbp_stealth
        from alfa_phone_stealth import create_alfa_phone_stealth

        # Alfa blocks ъЪёЁйЙ — pick safe long names without those.
        long_s = "Бахтияр Нурсултанович Юсупов"
        long_r = "Цветана Я."
        for name, fn, extra in (
            ("alfa_sbp", create_alfa_sbp_stealth, {"bank": "Сбербанк"}),
            ("alfa_phone", create_alfa_phone_stealth, {}),
        ):
            for label, sender, recv in (
                ("short", "Иван Петров", "Анна А."),
                ("long", long_s, long_r),
            ):
                with self.subTest(ch=name, label=label):
                    data = {
                        "date_time": "01.08.2026 12:00:00",
                        "amount": "5000",
                        "sender": sender,
                        "receiver": recv,
                        "phone": "+7 (999) 111-22-33",
                        "receipt_num": "авто",
                        **extra,
                    }
                    self._assert_pdf(fn(data), f"{name} {label}")

    def test_alfa_card_amount_only(self) -> None:
        from alfa_card_stealth import create_alfa_card_stealth

        pdf = create_alfa_card_stealth(
            {
                "date_time": "01.08.2026 12:00:00",
                "amount": "15000",
                "card": "2200 00** **** 1234",
                "receipt_num": "авто",
            }
        )
        self._assert_pdf(pdf, "alfa_card")

    def test_sber_sbp_phone_short_and_long(self) -> None:
        from sber_sbp_stealth import create_sber_sbp_stealth
        from sber_phone_stealth import create_sber_phone_stealth

        # Sber excludes ъЪёЁйЙ — use safe stress names.
        long_s = "Бахтияр Нурсултанович Юсупов"
        for name, fn in (
            ("sber_sbp", create_sber_sbp_stealth),
            ("sber_phone", create_sber_phone_stealth),
        ):
            for label, sender in (
                ("short", "Иван Иванович И."),
                ("long", long_s if name == "sber_phone" else "Катык Предсказуемович Е."),
            ):
                with self.subTest(ch=name, label=label):
                    data = {
                        "date_time": "01.08.2026 12:00:00",
                        "amount": "5000.00",
                        "sender": sender,
                        "receiver": "Анна А.",
                        "phone": "+7 (999) 111-22-33",
                        "bank": "Тинькофф",
                        "receipt_num": "авто",
                    }
                    self._assert_pdf(fn(data), f"{name} {label}")


if __name__ == "__main__":
    unittest.main()
