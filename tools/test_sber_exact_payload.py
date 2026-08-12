from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sber_dynamic as sd
import sber_glyph_library as sgl
import sber_phone_stealth as phone
import sber_sbp_stealth as sbp


class SberExactPayloadTests(unittest.TestCase):
    def test_required_charset_excludes_narrowed_letters_and_keeps_digits(self) -> None:
        required = set(sgl._FULL_REQUIRED)
        self.assertTrue(set("ъЪёЁйЙ").isdisjoint(required))
        self.assertTrue(set("0123456789").issubset(required))
        self.assertEqual(sgl.excluded_name_chars("Алена Ёлкина"), {"Ё"})

    def test_cmap_miss_never_substitutes_payload(self) -> None:
        payload = {
            "sender_name": "Федор Подячев",
            "receiver_name": "Алена Обедкова",
            "document_num": "1042876395",
            "auth_code": "406291",
        }
        self.assertEqual(sd._auto_fix_prepared_fields(payload, {"0": 1}), payload)

    def test_identity_fit_uses_only_trailing_padding(self) -> None:
        cmap = {ch: i + 10 for i, ch in enumerate("Федор Подячев ")}
        enc = lambda text: sd._sber_enc(text, {ord(k): v for k, v in cmap.items()})
        value = "Федор Подячев"
        hit = sd._fit_exact_identity(value, len(enc(value)) + 6, enc)
        self.assertIsNotNone(hit)
        fitted, _raw = hit
        self.assertEqual(fitted.rstrip(), value)
        self.assertFalse(fitted.startswith(" "))
        self.assertEqual(sd._name_slot_variants(value), [value])
        self.assertEqual(sd._bank_slot_variants("Газпромбанк"), ["Газпромбанк"])

    def test_date_fit_keeps_seconds_and_zone(self) -> None:
        value = "01 августа 2026 05:06:47 (МСК)"
        chars = set(value)
        cmap = {ord(ch): i + 10 for i, ch in enumerate(sorted(chars))}
        enc = lambda text: sd._sber_enc(text, cmap)
        fitted, _raw = sd._fit_text_encoded_length(
            value, len(enc(value.replace(" (МСК)", "(МСК)"))), enc,
            key_hint="date",
        )
        self.assertIn("05:06:47", fitted)
        self.assertTrue(fitted.endswith("(МСК)"))

    def test_valid_sbp_id_is_immutable(self) -> None:
        prepared = {
            "date_time": "01 августа 2026 05:06:47 (МСК)",
            "spb_number": "A6213020550029080G10070011650703",
        }
        sbp.sync_sbp_id_core_timestamp(prepared)
        self.assertEqual(prepared["spb_number"], "A6213020550029080G10070011650703")

    def test_sbp_failed_candidate_does_not_retry_changed_payload(self) -> None:
        data = {
            "sender_name": "Федор Подячев",
            "receiver_name": "Алена Обедкова",
            "amount": "10428",
            "phone": "+7 901 234-56-78",
            "bank_name": "Т-Банк",
            "date": "01.08.2026",
            "time": "05:06:47",
        }
        with patch("sber_dynamic.build_dynamic_sber_sbp", return_value=None) as build:
            self.assertIsNone(sbp.create_sber_sbp_stealth(data))
        build.assert_called_once()
        prepared = build.call_args.args[0]
        self.assertEqual(prepared["sender_name"], data["sender_name"])
        self.assertEqual(prepared["receiver_name"], data["receiver_name"])
        self.assertEqual(prepared["amount"], "10428.00  ₽")
        self.assertIn("05:06:47", prepared["date_time"])

    def test_phone_failed_candidate_does_not_retry_changed_payload(self) -> None:
        data = {
            "sender_name": "Федор Подячев",
            "receiver_name": "Алена Обедкова",
            "amount": "10428",
            "phone": "+7 901 234-56-78",
            "date": "01.08.2026",
            "time": "05:06:47",
            "document_num": "1042876395014287639",
            "auth_code": "406291",
        }
        with patch("sber_dynamic.build_dynamic_sber_phone", return_value=None) as build:
            self.assertIsNone(phone.create_sber_phone_stealth(data))
        build.assert_called_once()
        prepared = build.call_args.args[0]
        self.assertEqual(prepared["sender_name"], data["sender_name"])
        self.assertEqual(prepared["receiver_name"], data["receiver_name"])
        self.assertEqual(prepared["amount"], "10 428,00 ₽")
        self.assertEqual(prepared["document_num"], data["document_num"])
        self.assertEqual(prepared["auth_code"], data["auth_code"])
        self.assertIn("05:06:47", prepared["date_time"])

    def test_raw_shell_text_is_rejected(self) -> None:
        prepared = {
            "sender_name": "Федор Подячев",
            "receiver_name": "Алена Обедкова",
            "receiver_phone": "+7(901) 234-56-78",
            "amount": "10 428,00 ₽",
            "commission": "0,00 ₽",
            "document_num": "1042876395014287639",
            "auth_code": "406291",
            "date_time": "01 августа 2026 05:06:47 (МСК)",
        }
        self.assertFalse(phone._face_has_exact_fields("donor shell", prepared))


if __name__ == "__main__":
    unittest.main()
