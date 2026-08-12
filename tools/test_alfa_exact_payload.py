"""Focused local checks for Alfa exact-payload and charset invariants."""
from __future__ import annotations

import os
import sys
import unittest

import fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import alfa_card_stealth as card
import alfa_glyph_library as glyphs
import alfa_phone_stealth as phone
import alfa_sbp_stealth as sbp
from alfa_corpus import canonical_paths
from alfa_font_extend import _glyphs_ok_for_text, ensure_alfa_font_chars
from alfa_orig_mode import AlfaOrigContext


FULL_RU = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЫЬЭЮЯ"
BLOCKED_RU = "ъЪёЁйЙ"


class AlfaExactPayloadTests(unittest.TestCase):
    def assert_size_band(self, pdf: bytes, label: str) -> None:
        self.assertGreaterEqual(len(pdf), 58_000, label)
        self.assertLessEqual(len(pdf), 58_800, label)

    def test_fixed_left_column_anchors(self) -> None:
        for coords in (sbp.SBP_COORDS, card.CARD_COORDS, phone.PHONE_COORDS):
            self.assertEqual(coords["amount"][1], 35.45)

    def test_fixed_right_column_anchors(self) -> None:
        for key in ("phone", "recipient_bank", "account", "sbp_id", "message"):
            self.assertEqual(sbp.SBP_COORDS[key][1], 304.75)
        for key in ("receiver_card", "date_time", "operation_num"):
            self.assertEqual(card.CARD_COORDS[key][1], 304.75)
        for key in ("receiver", "phone", "account", "message"):
            self.assertEqual(phone.PHONE_COORDS[key][1], 304.75)

    def test_header_date_anchor_is_unchanged(self) -> None:
        expected = (779.15, 452.788)
        self.assertEqual(sbp.SBP_COORDS["date_formed"], expected)
        self.assertEqual(card.CARD_COORDS["date_formed"], expected)
        self.assertEqual(phone.PHONE_COORDS["date_formed"], expected)

    def test_sbp_manual_text_and_ids_are_exact(self) -> None:
        data = {
            "date_time": "01.08.2026 05:00:00",
            "receiver": "Элкин Подьем",
            "recipient_bank": "Банк Обьем-Э",
            "account": "ID-Э-09",
            "sbp_id": "aB-Э 019",
            "operation_num": "C16-MANUAL-Э09",
        }
        prepared = sbp._prepare_sbp(data)
        self.assertEqual(prepared["receiver"], data["receiver"])
        self.assertEqual(prepared["recipient_bank"], data["recipient_bank"])
        self.assertEqual(prepared["account"], data["account"])
        self.assertEqual(prepared["sbp_id"], data["sbp_id"])
        self.assertEqual(
            prepared["operation_num"].rstrip("\u00a0"),
            data["operation_num"],
        )

    def test_phone_keeps_supported_text_exact(self) -> None:
        prepared = phone._prepare_phone(
            {
                "date_time": "01.08.2026 05:00:00",
                "receiver": "Эдуард Егоров",
                "message": "Подьем Елки 09",
                "operation_num": "C07-MANUAL-Э09",
            }
        )
        self.assertEqual(
            prepared["message"].replace("\u00a0", " "),
            "Подьем Елки 09",
        )
        self.assertEqual(
            prepared["operation_num"].rstrip("\u00a0"),
            "C07-MANUAL-Э09",
        )
        self.assertEqual(
            prepared["receiver"].replace("\u00a0", " "),
            "Эдуард Егоров",
        )

    def test_glyph_library_covers_full_russian_and_digits(self) -> None:
        glyphs.ensure_library()
        available = glyphs.available_chars()
        required = set(FULL_RU + FULL_RU.lower() + "0123456789")
        self.assertEqual(required - available, set())
        sample = FULL_RU + FULL_RU.lower() + " 0123456789"
        self.assertEqual(sbp.check_text(sample), [])
        self.assertEqual(card.check_text(sample), [])
        self.assertEqual(phone.check_text(sample), [])
        for check in (sbp.check_text, card.check_text, phone.check_text):
            self.assertEqual(check(BLOCKED_RU), list(BLOCKED_RU))

    def test_live_generators_fail_closed_on_excluded_letters(self) -> None:
        for label, create in (
            ("sbp", sbp.create_alfa_sbp_stealth),
            ("card", card.create_alfa_card_stealth),
            ("phone", phone.create_alfa_phone_stealth),
        ):
            with self.subTest(label=label):
                self.assertIsNone(create({"receiver": "Тест йЁъ"}))

    def test_oracle_font_extension_keeps_rare_letters_and_digits(self) -> None:
        sample = "ЫыЭэЩщЦцЮю0123456789"
        for kind in ("sbp", "card"):
            pool = canonical_paths(kind)
            if not pool:
                self.skipTest(f"no canonical Alfa {kind} donor")
            extended = ensure_alfa_font_chars(pool[0], sample, pool)
            self.assertIsNotNone(extended, kind)
            ctx = AlfaOrigContext()
            self.assertTrue(ctx.load(extended), kind)
            self.assertTrue(
                _glyphs_ok_for_text(ctx, ctx.pdf_bytes, sample)[0],
                kind,
            )

    def test_phone_font_extension_keeps_rare_letters_and_digits(self) -> None:
        pool = canonical_paths("phone")
        if not pool:
            self.skipTest("no canonical Alfa phone donor")
        sample = "ЫыЭэЩщЦцЮю0123456789"
        extended = phone._ensure_phone_font_chars(pool[0], sample)
        self.assertIsNotNone(extended)
        ctx = phone.AlfaPhoneOrigContext()
        self.assertTrue(ctx.load(extended))
        self.assertTrue(phone._phone_glyphs_ok(ctx, sample)[0])

    def test_live_generators_emit_exact_fields_in_original_size_band(self) -> None:
        cases = (
            (
                "sbp",
                sbp.create_alfa_sbp_stealth,
                {
                    "amount": "7000",
                    "receiver": "Элкин Подьем",
                    "recipient_bank": "Банк Обьем-Э",
                    "phone": "+79001234567",
                    "account": "40817810000000000009",
                    "date_time": "01.08.2026 05:00:00",
                    "operation_num": "C160108260000009",
                    "sbp_id": "A61551545348731O0G10080011770901",
                },
                sbp.SBP_COORDS,
                sbp._prepare_sbp,
                AlfaOrigContext,
            ),
            (
                "card",
                card.create_alfa_card_stealth,
                {
                    "amount": "6000",
                    "sender_card": "220000******1234",
                    "receiver_card": "411111******9876",
                    "date_time": "01.08.2026 05:00:00",
                    "operation_num": "Z090108260000009",
                },
                card.CARD_COORDS,
                card._prepare_card,
                AlfaOrigContext,
            ),
            (
                "phone",
                phone.create_alfa_phone_stealth,
                {
                    "amount": "5500",
                    "receiver": "Эдуард Щукин",
                    "phone": "79271234568",
                    "account": "40817810123456780922",
                    "date_time": "01.08.2026 05:00:00",
                    "operation_num": "C070108260000009",
                    "message": "Подьем Елки 09",
                },
                phone.PHONE_COORDS,
                phone._prepare_phone,
                phone.AlfaPhoneOrigContext,
            ),
        )
        for label, create, data, coords, prepare, context_type in cases:
            with self.subTest(label=label):
                pdf = create(data)
                self.assertIsNotNone(pdf, label)
                self.assert_size_band(pdf, label)
                prepared = prepare(data)
                ctx = context_type()
                self.assertTrue(ctx.load_bytes(pdf), label)
                for key, (y, x) in coords.items():
                    got = (ctx.extract_at(y, x) or "").replace("\u00a0", " ").strip()
                    want = (prepared[key] or "").replace("\u00a0", " ").strip()
                    self.assertEqual(got, want, f"{label}:{key}")
                header = prepared["date_formed"].rstrip("\u00a0")
                if label == "phone":
                    doc = fitz.open(stream=pdf, filetype="pdf")
                    try:
                        hits = doc[0].search_for(header)
                    finally:
                        doc.close()
                    self.assertTrue(hits, label)
                    self.assertAlmostEqual(hits[0].x1, 559.69, delta=0.25)
                else:
                    header_width = sum(
                        ctx.widths[
                            ctx.uni_to_cid[0x00A0 if ch == " " else ord(ch)]
                        ]
                        for ch in header
                    )
                    header_right = (
                        coords["date_formed"][1] + header_width * 11 / 1000
                    )
                    self.assertAlmostEqual(header_right, 559.686, delta=0.02)
                text = "".join(prepared.values())
                if label == "phone":
                    self.assertTrue(phone._phone_glyphs_ok(ctx, text)[0], label)
                else:
                    self.assertTrue(
                        _glyphs_ok_for_text(ctx, ctx.pdf_bytes, text)[0],
                        label,
                    )


if __name__ == "__main__":
    unittest.main()
