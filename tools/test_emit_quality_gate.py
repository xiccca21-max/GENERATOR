# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import date

from tools.emit_quality_gate import (
    _amount_occurrences,
    _field_exact,
    _keywords_ok,
    _masked_display,
    _parse_tounicode,
    emit_ok,
)
from tools.receipt_gate_fixtures import (
    CYRILLIC,
    DIGITS,
    FULL_FIELD_CHARSET,
    LATIN,
    iter_charset_chunks,
    stress_payloads,
)
from tools.receipt_method_profiles import (
    FieldContract,
    METHOD_PROFILES,
    effective_size_band,
)


class ProfileTests(unittest.TestCase):
    def test_all_ten_live_methods_have_profiles(self) -> None:
        self.assertEqual(
            set(METHOD_PROFILES),
            {
                "tbank_sbp", "tbank_phone", "tbank_card_sber",
                "tbank_card_tbank", "tbank_nocomm", "sber_sbp",
                "sber_phone", "alfa_sbp", "alfa_card", "alfa_phone",
            },
        )

    def test_profiles_have_references_bands_anchors_and_contracts(self) -> None:
        for method, profile in METHOD_PROFILES.items():
            with self.subTest(method=method):
                self.assertTrue(profile.references)
                self.assertTrue(profile.corpus_roots)
                self.assertTrue(
                    any(path.is_file() for path in profile.references),
                    f"no original reference for {method}",
                )
                self.assertTrue(
                    all(path.is_dir() for path in profile.corpus_roots),
                    f"original corpus unavailable for {method}",
                )
                self.assertLess(profile.size_band[0], profile.size_band[1])
                self.assertIn("date", profile.anchors)
                self.assertIn("amount", profile.fields)
                self.assertTrue(profile.fields["amount"].required)
                lo, hi = effective_size_band(profile)
                self.assertLessEqual(lo, hi)

    def test_stress_fixture_satisfies_required_contracts(self) -> None:
        for chunk in iter_charset_chunks():
            payloads = stress_payloads(chunk)
            for method, profile in METHOD_PROFILES.items():
                payload = payloads[method]
                for field, contract in profile.fields.items():
                    if contract.required:
                        with self.subTest(method=method, field=field, chunk=chunk):
                            self.assertTrue(payload.get(field) or field == "date_time")


class CharsetFixtureTests(unittest.TestCase):
    def test_fixture_covers_exact_required_charset(self) -> None:
        joined = "".join(iter_charset_chunks(7))
        self.assertEqual(joined, FULL_FIELD_CHARSET)
        self.assertTrue(set(CYRILLIC) <= set(joined))
        self.assertTrue(set(LATIN) <= set(joined))
        self.assertTrue(set(DIGITS) <= set(joined))
        self.assertFalse(set("ъЪёЁйЙ") & set(joined))


class ExactFieldTests(unittest.TestCase):
    def test_fio_prefix_does_not_pass(self) -> None:
        ok, _ = _field_exact(
            "Получатель\nИван",
            {"receiver": "Иванов Петр"},
            "receiver",
            FieldContract("fio", required=True),
        )
        self.assertFalse(ok)

    def test_phone_last_four_does_not_pass(self) -> None:
        ok, _ = _field_exact(
            "Телефон\n***-**-4567",
            {"phone": "+7 (916) 123-45-67"},
            "phone",
            FieldContract("phone", required=True),
        )
        self.assertFalse(ok)

    def test_explicit_phone_mask_passes_only_full_mask(self) -> None:
        contract = FieldContract("phone", required=True, display_mask="alfa_phone")
        ok, _ = _field_exact(
            "Номер телефона получателя\n916***4567",
            {"phone": "+7 (916) 123-45-67"},
            "phone",
            contract,
        )
        self.assertTrue(ok)
        bad, _ = _field_exact(
            "Номер телефона получателя\n915***4567",
            {"phone": "+7 (916) 123-45-67"},
            "phone",
            contract,
        )
        self.assertFalse(bad)

    def test_card_masks_are_deterministic(self) -> None:
        self.assertEqual(
            _masked_display("2202201234560942", "card_6_6_4"),
            "220220******0942",
        )
        self.assertEqual(
            _masked_display("2202201234567015", "card_last4"),
            "*7015",
        )

    def test_repeated_amount_requires_both_values(self) -> None:
        contract = FieldContract("amount", required=True, repeats=2)
        one, _ = _field_exact(
            "Сумма\n75 670 i",
            {"amount": "75670"},
            "amount",
            contract,
        )
        self.assertFalse(one)
        two, _ = _field_exact(
            "Итого 75 670 i\nСумма 75 670 i\nКомиссия 0 i",
            {"amount": "75670"},
            "amount",
            contract,
        )
        self.assertTrue(two)
        self.assertEqual(len(_amount_occurrences("79 070 i\n79 070 i", "75670")), 0)

    def test_id_prefix_does_not_pass(self) -> None:
        ok, _ = _field_exact(
            "A615515",
            {"sbp_id": "A61551545348731O0G10080011770901"},
            "sbp_id",
            FieldContract("id", required=True),
        )
        self.assertFalse(ok)


class MetadataAndCmapTests(unittest.TestCase):
    class _Doc:
        def __init__(self, keywords: str):
            self.metadata = {"keywords": keywords}

    def test_tbank_keywords_cutover(self) -> None:
        profile = METHOD_PROFILES["tbank_sbp"]
        before, _ = _keywords_ok(
            self._Doc("01.07.2026 | hash | 991"),
            profile,
            {"date_time": "09.07.2026 12:00:00"},
        )
        after, _ = _keywords_ok(
            self._Doc("10.07.2026 | hash | DOCS-2035"),
            profile,
            {"date_time": "10.07.2026 12:00:00"},
        )
        wrong, _ = _keywords_ok(
            self._Doc("10.07.2026 | hash | 991"),
            profile,
            {"date_time": "10.07.2026 12:00:00"},
        )
        self.assertTrue(before)
        self.assertTrue(after)
        self.assertFalse(wrong)

    def test_tounicode_bfchar_and_range(self) -> None:
        cmap = _parse_tounicode(
            b"2 beginbfchar\n<0001> <0410>\n<0002> <0411>\nendbfchar\n"
            b"1 beginbfrange\n<0010> <0012> <0030>\nendbfrange"
        )
        self.assertEqual(cmap[1], "А")
        self.assertEqual(cmap[2], "Б")
        self.assertEqual([cmap[16], cmap[17], cmap[18]], ["0", "1", "2"])

    def test_unknown_method_fails_closed_before_pdf_parsing(self) -> None:
        ok, why = emit_ok(b"%PDF-not-a-real-document", bank_hint="other", expect={"amount": "1"})
        self.assertFalse(ok)
        self.assertIn("unknown-method-profile", why)


if __name__ == "__main__":
    unittest.main()
