"""Focused local checks for Alfa exact-payload and charset invariants."""
from __future__ import annotations

import os
import re
import struct
import sys
import unittest
from datetime import datetime

import fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import alfa_card_stealth as card
import alfa_emit
import alfa_glyph_library as glyphs
import alfa_phone_stealth as phone
import alfa_sbp_stealth as sbp
from alfa_corpus import canonical_paths
from alfa_font_extend import _glyphs_ok_for_text, ensure_alfa_font_chars
from alfa_orig_mode import AlfaOrigContext


FULL_RU = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЫЬЭЮЯ"
BLOCKED_RU = "ъЪёЁйЙ"


class AlfaExactPayloadTests(unittest.TestCase):
    def assert_natural_fontfile2(self, pdf: bytes, label: str) -> None:
        self.assertTrue(sbp._fontfile2_has_exact_sfnt_end(pdf), label)

    def test_sfnt_release_gate_rejects_trailing_bytes(self) -> None:
        # One table at offset 28, length 1; the natural aligned SFNT end is 32.
        sfnt = (
            b"\x00\x01\x00\x00"
            + struct.pack(">HHHH", 1, 16, 0, 0)
            + b"head"
            + struct.pack(">III", 0, 28, 1)
            + b"\0\0\0\0"
        )
        self.assertEqual(alfa_emit.sfnt_aligned_end(sfnt), 32)
        self.assertTrue(alfa_emit.sfnt_has_exact_aligned_end(sfnt))
        self.assertFalse(alfa_emit.sfnt_has_exact_aligned_end(sfnt + b"tail"))

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

    def test_sbp_fio_is_name_patronymic_initial(self) -> None:
        self.assertEqual(
            sbp._fmt_alfa_sbp_receiver("Алина Александровна А"),
            "Алина Александровна А",
        )
        self.assertEqual(
            sbp._fmt_alfa_sbp_receiver("Диана Камильевна П."),
            "Диана Камильевна П",
        )
        self.assertEqual(sbp._fmt_alfa_sbp_receiver(""), "Алина Александровна А")
        two = sbp._fmt_alfa_sbp_receiver("Павел Соколов")
        self.assertRegex(two, r"^Павел \S+(ович|евич) С$")
        short = sbp._fmt_alfa_sbp_receiver("Анна А.")
        self.assertRegex(short, r"^Анна \S+(овна|евна) А$")

    def test_card_never_emits_blank_pan(self) -> None:
        blank = card._prepare_card({"amount": "1000", "date_time": "сейчас"})
        for key in ("sender_card", "receiver_card"):
            compact = re.sub(r"[\s\u00a0]", "", blank[key])
            self.assertRegex(compact, r"^220\d{3}\*{6}\d{4}$", key)
        kept = card._fmt_card("2200151234568946")
        self.assertEqual(kept, "220015******8946")

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
        self.assertEqual(prepared["receiver"], sbp._fmt_alfa_sbp_receiver("Элкин Подьем"))
        self.assertRegex(prepared["receiver"], r"^Элкин \S+(ович|евич) П$")
        self.assertEqual(prepared["recipient_bank"], data["recipient_bank"])
        self.assertRegex(prepared["account"], r"^40817\d{15}$")
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

    def test_live_generators_emit_exact_fields_with_natural_fontfile2(self) -> None:
        cases = (
            (
                "sbp",
                sbp.create_alfa_sbp_stealth,
                {
                    "amount": "7000",
                    "receiver": "Элкин Петрович П",
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
                self.assert_natural_fontfile2(pdf, label)
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


    def test_op_tail_follows_hour_profile_not_evening_band(self) -> None:
        dt = datetime(2026, 8, 10, 10, 48, 15)
        self.assertEqual(sbp._op_tail_center(dt), 569543)
        op = sbp._gen_sbp_op_num(dt)
        self.assertTrue(op.startswith("C16100826"))
        tail = int(op[9:])
        self.assertGreater(tail, 500_000)
        self.assertLess(tail, 650_000)
        self.assertNotEqual(tail, sbp._op_tail_center(dt))

    def test_generated_account_uses_live_ledger_family(self) -> None:
        for _ in range(20):
            account = sbp._gen_alfa_debit_account()
            self.assertIn(account[9:12], sbp._ALFA_LIVE_LEDGERS)
            self.assertEqual(sbp._ru_account_checksum(sbp._ALFA_PAYER_BIK, account), 0)

    def test_sbp_utc_is_local_minus_3h_with_3_to_5s_lag(self) -> None:
        dt = datetime(2026, 8, 10, 10, 48, 15)
        self.assertEqual(sbp._sbp_utc_clock(dt, 5), datetime(2026, 8, 10, 7, 48, 10))
        self.assertEqual(sbp._sbp_lag_bounds(dt), (3, 5))
        self.assertTrue(
            sbp._sbp_id_model_ok(
                "B62220748109111V0B10130011821301", dt, "Т-Банк",
            )
        )
        self.assertEqual(sbp._bank_route("Т-Банк"), ("B", "B10130011821301"))


if __name__ == "__main__":
    unittest.main()
