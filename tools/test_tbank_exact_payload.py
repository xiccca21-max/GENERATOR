"""Focused local invariants for all five live T-Bank generators."""

from __future__ import annotations

import inspect
import os
import sys
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tbank_card_tbank_stealth as card_tbank
import tbank_dynamic
import tbank_nocomm_stealth as nocomm
import tbank_phone_stealth as phone
import tbank_sbp_stealth as sbp
import tbank_stealth_v3 as card_sber


class ExactPayloadTests(unittest.TestCase):
    def test_keywords_token_follows_face_date(self) -> None:
        self.assertEqual(sbp._keywords_third_token(datetime(2026, 7, 9)), "991")
        self.assertEqual(
            sbp._keywords_third_token(datetime(2026, 7, 10)), "DOCS-2035"
        )

    def test_sbp_preserves_manual_text_and_ids(self) -> None:
        opid = "A1234567890123456789012345678901"
        prepared = sbp._prepare_sbp_data(
            {
                "date_time": "11.07.2026  12:34:56",
                "amount": "987654",
                "sender": "Фёдор Подъёмов",
                "recipient": "Алёна Объектова",
                "recipient_bank": "Банк Ёжъ",
                "phone": "+7 (999) 123-45-67",
                "account": "40817810000000001234",
                "sbp_id": opid,
                "sbp_suffix": "ABCDE",
                "receipt_num": "1-123-456-789-012",
            }
        )
        self.assertIsNotNone(prepared)
        assert prepared is not None
        self.assertEqual(prepared["sender"], "Фёдор Подъёмов")
        self.assertEqual(prepared["receiver"], "Алёна Объектова")
        self.assertEqual(prepared["bank"], "Банк Ёжъ")
        self.assertEqual(prepared["sbp_id_raw"], opid)
        self.assertEqual(prepared["sbp_suffix_raw"], "ABCDE")
        self.assertEqual(prepared["receipt_raw"], "1-123-456-789-012")

    def test_other_channels_preserve_names(self) -> None:
        common = {
            "date_time": "11.07.2026  12:34:56",
            "amount": "987654",
            "sender": "Фёдор Подъёмов",
            "receipt_num": "1-123-456-789-012",
        }
        p_phone = phone._prepare_phone_data(
            {**common, "receiver": "Алёна Объектова", "phone": "+7 (999) 123-45-67"}
        )
        p_cs = card_sber._prepare_card_data(
            {
                **common,
                "receiver": "Алёна Объектова",
                "recipient_bank": "Банк Ёжъ",
                "card": "220000******1234",
            }
        )
        p_ct = card_tbank._prepare_ct_data(
            {**common, "receiver": "Алёна Объектова", "card": "*1234"}
        )
        p_nc = nocomm._prepare_nc_data({**common, "card": "220000******1234"})
        for prepared in (p_phone, p_cs, p_ct, p_nc):
            self.assertEqual(prepared["sender"], "Фёдор Подъёмов")
        self.assertEqual(p_phone["receiver"], "Алёна Объектова")
        self.assertEqual(p_cs["receiver"], "Алёна Объектова")
        self.assertEqual(p_cs["bank"], "Банк Ёжъ")
        self.assertEqual(p_ct["receiver"], "Алёна Объектова")

    def test_live_pipeline_has_no_payload_retry_mutation(self) -> None:
        source = inspect.getsource(tbank_dynamic.create_tbank_pipeline)
        self.assertNotIn('prepared = {**prepared, "receipt_num": "авто"}', source)
        self.assertNotIn("soft-pass", source)
        sbp_source = inspect.getsource(sbp.create_tbank_sbp_stealth)
        self.assertNotIn("EMERGENCY", sbp_source)
        self.assertNotIn("_sender_soft_variants", sbp_source)

    def test_phone_emits_short_receiver_and_donor_name_payload(self) -> None:
        """Regression: bot «не собралось» on short FIO / same-as-donor sender.

        Exact-flate used to miss after amount CID-pad ban; layout gate falsely
        rejected user-requested «Дамир Сеничев». Must emit PDF, not None.
        """
        payloads = (
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
                "sender": "Мухаммад Абдулвахитович Алиев",
                "receiver": "Мухаммад А.",
                "phone": "+7 (999) 222-33-44",
                "receipt_num": "авто",
            },
        )
        for payload in payloads:
            with self.subTest(receiver=payload["receiver"], sender=payload["sender"]):
                pdf = phone.create_tbank_phone_stealth(payload)
                self.assertIsNotNone(pdf, msg=f"emit None for {payload}")
                assert pdf is not None
                self.assertTrue(pdf.startswith(b"%PDF-"))
                self.assertGreater(len(pdf), 50_000)
                self.assertLessEqual(len(pdf), phone._PHONE_HARD_MAX)


if __name__ == "__main__":
    unittest.main()
