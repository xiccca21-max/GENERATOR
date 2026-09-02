"""Focused local invariants for all five live T-Bank generators."""

from __future__ import annotations

import inspect
import os
import re
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
        self.assertEqual(sbp._keywords_third_token(datetime(2026, 6, 29)), "991")

    def test_sbp_profile_avoids_791103_014_combo(self) -> None:
        prof = sbp._sbp_bank_profile("Сбербанк", "24781", "29.06.2026 12:00:42")
        self.assertEqual(prof["suffix"], "91103")
        self.assertNotEqual(prof["ch"], "1014")
        sid = sbp._gen_sbp_id(
            "29.06.2026 12:00:42",
            bank="Прайм Капитал",
            amount="24781",
        )
        self.assertTrue(sbp.verify_sbp_id_date(sid, "29.06.2026 12:00:42"))
        flat = sid + "91103"
        self.assertNotEqual(
            sbp._sbp_tuple7_from_flat(flat),
            "0|5|0|G1|014|00117|791103",
        )
        burned = "B6180165841075050G101400117"
        fixed = sbp._ensure_sbp_linked_tuple(burned, "91103")
        self.assertNotEqual(
            sbp._sbp_tuple7_from_flat(fixed + "91103"),
            "0|5|0|G1|014|00117|791103",
        )

    def test_sbp_g1_slot018_binding(self) -> None:
        prof = sbp._sbp_bank_profile("Сбербанк", "2500", "14.08.2026 12:00:00")
        self.assertEqual(prof["ch"], "1018")
        sid = sbp._gen_sbp_id(
            "14.08.2026 12:00:00",
            bank="Сбербанк",
            amount="2500",
        )
        flat = sid + prof["suffix"]
        self.assertEqual(flat[19:22], "018")
        self.assertIn((sid[14], sid[15]), {("0", "H"), ("1", "S")})
        bad = list(sid)
        bad[14], bad[15] = "A", "5"
        fixed = sbp._enforce_g1_slot018_binding("".join(bad), prof["suffix"])
        self.assertIn((fixed[14], fixed[15]), {("0", "H"), ("1", "S")})

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

    def test_f2_cap_never_drops_itogo(self) -> None:
        cmap = {
            ord("1"): 306, ord("2"): 307, ord("7"): 312, ord("6"): 311,
            ord("4"): 309, ord(" "): 3,
            ord("И"): 244, ord("т"): 287, ord("о"): 283, ord("г"): 271,
        }
        out = sbp._cap_f2_tounicode_under_v3(cmap, "12 764", max_entries=9)
        for ch in "Итого":
            self.assertIn(ord(ch), out, msg=f"dropped {ch!r} from F2 cmap")

    def test_unused_cmap_remap_keeps_itogo_and_fio(self) -> None:
        keep = sbp._tbank_keep_face_uniscodes(
            {
                "_user_sender": "Владислав Мафаня",
                "_user_receiver": "Анастасия Д.",
                "sender": "Зладислав Мафаня",
                "receiver": "Анастасия ,.",
            }
        )
        for ch in "ИтогоВДгВладислав":
            self.assertIn(ch, keep, msg=f"must keep {ch!r}")

    def test_face_keep_canonical_a_even_if_plan_maps_to_e(self) -> None:
        """Андреев's А (GID 235) must stay protected even if plan bound it to Е."""
        keep = sbp._face_keep_cids(
            {
                "_user_sender": "Юруслан Андреев",
                "sender": "Юруслан Ендреев",
                "_user_receiver": "Кабидзе Д.",
            },
            {ord("А"): 240, ord("Е"): 240},
        )
        self.assertIn(235, keep)
        self.assertIn(240, keep)
        self.assertEqual(sbp.TBANK_CHAR_TO_GID_REG["А"], 235)
        self.assertEqual(sbp.TBANK_CHAR_TO_GID_REG["Е"], 240)

    def test_filled_uni_map_never_falls_back_to_empty_cid(self) -> None:
        """Empty canonical А must not be encoded as that CID (→ later unpaint to Е)."""
        out = sbp._filled_uni_map(
            b"not-a-font",
            {ord("А"), ord("Е")},
            [{ord("А"): 235, ord("Е"): 240}],
            is_medium=False,
        )
        self.assertNotIn(ord("А"), out)
        self.assertNotIn(ord("Е"), out)

    def test_sbp_force_face_helpers_exist(self) -> None:
        self.assertTrue(callable(sbp._sbp_force_correct_face))
        self.assertTrue(callable(sbp._tbank_force_correct_face))
        self.assertTrue(callable(sbp._sbp_hydrate_letters_inplace))

    def test_all_tbank_methods_use_sbp_structure_gate(self) -> None:
        import tbank_statement_stealth as stmt

        fin = inspect.getsource(sbp._sbp_finalize_for_ship)
        self.assertIn("channel", fin)
        self.assertIn("apply_sbp_font_hard_fixes", fin)
        self.assertIn("_sbp_clear_v3_reassembly_family", fin)
        self.assertIn("_sbp_enforce_structure_gate", fin)
        self.assertNotIn("if sbp:", fin)
        gate = inspect.getsource(sbp._sbp_enforce_structure_gate)
        self.assertIn("channel", gate)
        finish = inspect.getsource(tbank_dynamic._tbank_finish_non_sbp_ship)
        self.assertIn("_sbp_finalize_for_ship", finish)
        self.assertIn("_tbank_force_correct_face", finish)
        self.assertIn("_tbank_finish_non_sbp_ship", inspect.getsource(phone.create_tbank_phone_stealth))
        self.assertIn("_tbank_finish_non_sbp_ship", inspect.getsource(nocomm.create_tbank_nocomm_stealth))
        self.assertIn("_tbank_finish_non_sbp_ship", inspect.getsource(stmt.create_tbank_statement))
        self.assertIn("create_tbank_pipeline", inspect.getsource(card_tbank.create_tbank_card_tbank_stealth))
        self.assertIn("_tbank_finish_non_sbp_ship", inspect.getsource(card_sber.create_tbank_stealth))

    def test_f1_cmap_proton_band_471_not_v3_12790(self) -> None:
        """Card height=471 uses corpus atlas ±400, not SBP V3 12790."""
        band = sbp._f1_cmap_proton_band(63, 471)
        self.assertEqual(band, (11732 - 400, 11732 + 400))
        self.assertLess(band[1], 12790)
        band61 = sbp._f1_cmap_proton_band(61, 471)
        self.assertEqual(band61, (11614 - 400, 11620 + 400))
        self.assertIsNone(sbp._f1_cmap_proton_band(99, 471))

    def test_v3_clear_noop_on_card_sber_original(self) -> None:
        path = os.path.join(ROOT, "templates", "T_card_sber_original.pdf")
        if not os.path.isfile(path):
            self.skipTest("T_card_sber_original.pdf missing")
        with open(path, "rb") as fh:
            pdf = fh.read()
        import fitz
        import tbank_unlock_template as tut
        doc = fitz.open(stream=pdf, filetype="pdf")
        self.assertEqual(int(round(float(doc[0].mediabox.y1))), 471)
        meta = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        g0 = sbp._glyf_table_length(doc.xref_stream(meta["fontfile_xref"]))
        doc.close()
        out = sbp._sbp_clear_v3_reassembly_family(pdf)
        doc = fitz.open(stream=out, filetype="pdf")
        meta = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        g1 = sbp._glyf_table_length(doc.xref_stream(meta["fontfile_xref"]))
        doc.close()
        self.assertEqual(g1, g0)
        self.assertLess(g1, 12132)

    def test_force_face_non_sbp_skips_slot_realign(self) -> None:
        src = inspect.getsource(sbp._tbank_force_correct_face)
        self.assertIn("rewrite_sbp_slots=(ch == \"sbp\")", src)
        self.assertNotIn("del channel", src)

    def test_value_lattice_snap_exists(self) -> None:
        from tbank_emit import polish_layout_pdf, snap_value_column_lattice_pdf
        src = inspect.getsource(polish_layout_pdf)
        self.assertIn("snap_value_column_lattice_pdf", src)
        self.assertTrue(callable(snap_value_column_lattice_pdf))
        self.assertTrue(callable(__import__("tbank_emit", fromlist=["value_column_lattice_off"]).value_column_lattice_off))
        # Card polish must not run the +0.006 overshoot nudge (fights {-1,0}).
        sbp_if = src.find('if channel == "sbp"')
        card_elif = src.find('"card_sber"')
        self.assertGreater(card_elif, sbp_if)
        self.assertGreater(
            src.find("snap_value_column_lattice_pdf", card_elif),
            card_elif,
        )
        self.assertLess(
            src.find("nudge_value_column_pdf"),
            card_elif,
        )
        self.assertEqual(
            src.find("nudge_value_column_pdf", card_elif),
            -1,
        )

    def test_fmt_date_treats_auto_as_now(self) -> None:
        out = sbp._fmt_date("авто")
        self.assertRegex(out, r"^\d{2}\.\d{2}\.\d{4}  \d{2}:\d{2}:\d{2}$")
        self.assertFalse(out.startswith("авто"))
        v3 = card_sber._fmt_date("auto")
        self.assertRegex(v3, r"^\d{2}\.\d{2}\.\d{4}  \d{2}:\d{2}:\d{2}$")

    def test_bot_sbp_rest_omitted_bank_seychas(self) -> None:
        bank, date, tail = sbp._parse_tbank_sbp_rest(["сейчас", "авто", "авто"])
        self.assertEqual(bank, "Сбербанк")
        self.assertEqual(date, "сейчас")
        self.assertEqual(tail, ["авто", "авто"])
        bank, date, tail = sbp._parse_tbank_sbp_rest(["ВТБ", "сейчас"])
        self.assertEqual(bank, "ВТБ")
        self.assertEqual(date, "сейчас")
        self.assertEqual(tail, [])
        opid = "B6196011155964410B101300117"
        bank, date, tail = sbp._parse_tbank_sbp_rest([opid, "авто"])
        self.assertEqual(bank, "Сбербанк")
        self.assertEqual(date, "сейчас")
        self.assertEqual(tail, [opid, "авто"])

    def test_f1_maxp_hhea_envelopes_restored_after_subset_recompute(self) -> None:
        import hashlib

        import fitz
        import tbank_unlock_template as tut

        path = os.path.join(ROOT, "templates", "T_sbp_original.pdf")
        doc = fitz.open(path)
        ff = doc.xref_stream(
            tut._find_font_objects(doc)["TinkoffSans-Regular"]["fontfile_xref"]
        )
        doc.close()
        maxp = bytearray(sbp._get_font_table(ff, b"maxp"))
        maxp[6:8] = (85).to_bytes(2, "big")
        maxp[8:10] = (4).to_bytes(2, "big")
        maxp[10:12] = (51).to_bytes(2, "big")
        maxp[12:14] = (3).to_bytes(2, "big")
        maxp[28:30] = (1).to_bytes(2, "big")
        broken = sbp._restore_font_table(ff, b"maxp", bytes(maxp))
        broken = sbp._restore_font_table(broken, b"hhea", b"\x00" * 36)
        pinned = sbp._pin_f1_head_flags_and_csa(broken)
        mp = sbp._get_font_table(pinned, b"maxp")
        self.assertEqual(int.from_bytes(mp[6:8], "big"), 100)
        self.assertEqual(int.from_bytes(mp[8:10], "big"), 7)
        self.assertEqual(int.from_bytes(mp[10:12], "big"), 102)
        self.assertEqual(int.from_bytes(mp[12:14], "big"), 4)
        self.assertEqual(int.from_bytes(mp[28:30], "big"), 3)
        hhea = sbp._get_font_table(pinned, b"hhea")
        self.assertEqual(
            hashlib.sha256(hhea).hexdigest()[:16], "08c0a1c91858beca"
        )

    def test_randomize_subset_tags_never_reuse_corpus_prefix(self) -> None:
        import hashlib

        import fitz
        import tbank_unlock_template as tut

        path = os.path.join(ROOT, "templates", "T_sbp_original.pdf")
        with open(path, "rb") as fh:
            pdf = fh.read()
        doc0 = fitz.open(stream=pdf, filetype="pdf")
        fonts0 = tut._find_font_objects(doc0)
        ff_sha = {
            name: hashlib.sha256(doc0.xref_stream(meta["fontfile_xref"])).digest()
            for name, meta in fonts0.items()
        }
        doc0.close()
        out = sbp._randomize_pdf_fingerprints(pdf)
        tags = set(re.findall(rb"([A-Z]{6})\+(?:TinkoffSans|ALSRubl)", out))
        corpus = tbank_dynamic.corpus_tbank_subset_tags()
        self.assertTrue(tags)
        self.assertTrue(tags.isdisjoint(corpus))
        doc1 = fitz.open(stream=out, filetype="pdf")
        fonts1 = tut._find_font_objects(doc1)
        fitz_tags = set()
        for name, meta in fonts1.items():
            obj = doc1.xref_object(int(meta["type0_xref"]))
            m = re.search(r"/([A-Z]{6})\+(?:TinkoffSans|ALSRubl)", obj)
            self.assertIsNotNone(m, name)
            fitz_tags.add(m.group(1).encode("ascii"))
            self.assertEqual(
                hashlib.sha256(doc1.xref_stream(meta["fontfile_xref"])).digest(),
                ff_sha[name],
                msg=f"FontFile2 mutated for {name}",
            )
        doc1.close()
        self.assertTrue(fitz_tags)
        self.assertTrue(fitz_tags.isdisjoint(corpus))

    def test_corpus_f1_twin_lookup_is_cached(self) -> None:
        import time

        tbank_dynamic._ensure_corpus_f1_twin_index()
        a = tbank_dynamic._load_corpus_f1_twin_for_glyf(12530)
        t0 = time.perf_counter()
        b = tbank_dynamic._load_corpus_f1_twin_for_glyf(12530)
        elapsed = time.perf_counter() - t0
        self.assertEqual(a, b)
        self.assertLess(elapsed, 0.05)

    def test_value_column_spread_pin_on_live_fail_pdf(self) -> None:
        from tbank_emit import polish_layout_pdf, value_column_spread

        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (5).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        self.assertGreater(value_column_spread(pdf), 2.0)
        out = polish_layout_pdf(pdf, channel="sbp")
        self.assertLess(value_column_spread(out), 0.05)

    def test_sync_amount_slots_and_bbox_on_live_fail7(self) -> None:
        import fitz
        import tbank_unlock_template as tut

        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (7).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (7) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        prep = {"new_amount": "37 648 "}
        text0 = fitz.open(stream=pdf, filetype="pdf")[0].get_text()
        self.assertIn("24 780", text0)
        out = sbp._sbp_sync_amount_slots(pdf, prep)
        out = sbp._sbp_pin_f1_head_bbox_to_fontbbox(out)
        doc = fitz.open(stream=out, filetype="pdf")
        text = doc[0].get_text()
        self.assertNotIn("24 780", text)
        self.assertGreaterEqual(text.count("37 648"), 2)
        fr = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        fd = doc.xref_object(int(fr["fontdesc_xref"]))
        m = re.search(
            r"/FontBBox\s*\[\s*([-+]?\d+)\s+([-+]?\d+)\s+"
            r"([-+]?\d+)\s+([-+]?\d+)\s*\]",
            fd,
        )
        self.assertIsNotNone(m)
        want = tuple(int(m.group(i)) for i in range(1, 5))
        have = sbp._head_bbox_from_ff2(doc.xref_stream(int(fr["fontfile_xref"])))
        doc.close()
        self.assertEqual(have, want)

    def test_cmap_band_clamp_on_live_fail13(self) -> None:
        import fitz
        import tbank_unlock_template as tut

        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (13).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (13) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        doc0 = fitz.open(stream=pdf, filetype="pdf")
        fr0 = tut._find_font_objects(doc0)["TinkoffSans-Regular"]
        g0 = sbp._glyf_table_length(doc0.xref_stream(int(fr0["fontfile_xref"])))
        doc0.close()
        self.assertGreater(g0, 13402)
        out = sbp._sbp_clamp_f1_cmap_band_pdf(pdf)
        empty = sbp._pdf_f1_empty_painted_chars(out)
        doc = fitz.open(stream=out, filetype="pdf")
        fr = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        ff = doc.xref_stream(int(fr["fontfile_xref"]))
        tu = tut._parse_subset_tounicode(
            doc.xref_stream(int(fr["tounicode_xref"])).decode("latin1", "replace")
        )
        g = sbp._glyf_table_length(ff)
        text = doc[0].get_text()
        doc.close()
        band = sbp._f1_cmap_proton_band(len(tu))
        self.assertIsNotNone(band)
        self.assertLessEqual(g, band[1])
        self.assertGreaterEqual(g, band[0])
        self.assertEqual(empty, [])
        self.assertIn("Николай Звягинцев", text)
    def test_v3_c_clause_fats_f2_on_live_fail14(self) -> None:
        import fitz
        import tbank_unlock_template as tut

        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (14).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (14) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        doc0 = fitz.open(stream=pdf, filetype="pdf")
        fm0 = tut._find_font_objects(doc0)
        fr0 = fm0["TinkoffSans-Regular"]
        fm20 = fm0["TinkoffSans-Medium"]
        ff10 = doc0.xref_stream(int(fr0["fontfile_xref"]))
        ff20 = doc0.xref_stream(int(fm20["fontfile_xref"]))
        tu10 = doc0.xref_stream(int(fr0["tounicode_xref"]))
        tu20 = doc0.xref_stream(int(fm20["tounicode_xref"]))
        f1g0 = sbp._glyf_table_length(ff10)
        f2g0 = sbp._glyf_table_length(ff20)
        _m1, f1bf0, _ = sbp._parse_tounicode_counts(tu10)
        _m2, _, f2br0 = sbp._parse_tounicode_counts(tu20)
        f1_raw0, _ = sbp._xref_stream_raw_decoded(pdf, int(fr0["fontfile_xref"]))
        tu2_raw0, tu2_dec0 = sbp._xref_stream_raw_decoded(
            pdf, int(fm20["tounicode_xref"]),
        )
        doc0.close()
        self.assertTrue(
            sbp._v3_font_signature(f1g0, f2g0, f1bf0, f2br0)
            and sbp._v3_stream_signature(
                f1_raw0, len(ff10), tu2_raw0, len(tu2_dec0 or tu20),
            )
        )
        self.assertGreater(f1g0, 12783)
        self.assertLessEqual(f2g0, 1549)
        out = sbp._sbp_clear_v3_reassembly_family(pdf)
        doc = fitz.open(stream=out, filetype="pdf")
        fm = tut._find_font_objects(doc)
        fr = fm["TinkoffSans-Regular"]
        fm2 = fm["TinkoffSans-Medium"]
        ff1 = doc.xref_stream(int(fr["fontfile_xref"]))
        ff2 = doc.xref_stream(int(fm2["fontfile_xref"]))
        tu1 = doc.xref_stream(int(fr["tounicode_xref"]))
        tu2 = doc.xref_stream(int(fm2["tounicode_xref"]))
        cs = doc.xref_stream(doc[0].get_contents()[0])
        text = doc[0].get_text()
        f1g = sbp._glyf_table_length(ff1)
        f2g = sbp._glyf_table_length(ff2)
        _m1b, f1bf, _ = sbp._parse_tounicode_counts(tu1)
        _m2b, _, f2br = sbp._parse_tounicode_counts(tu2)
        f1_raw, _ = sbp._xref_stream_raw_decoded(out, int(fr["fontfile_xref"]))
        tu2_raw, tu2_dec = sbp._xref_stream_raw_decoded(
            out, int(fm2["tounicode_xref"]),
        )
        head2 = sbp._get_font_table(ff2, b"head")
        doc.close()
        self.assertFalse(
            sbp._v3_font_signature(f1g, f2g, f1bf, f2br)
            and sbp._v3_stream_signature(
                f1_raw, len(ff1), tu2_raw, len(tu2_dec or tu2),
            )
        )
        self.assertEqual(int(f2g), 1554)
        self.assertEqual(head2, sbp._TBANK_F2_JASPER_HEAD)
        self.assertEqual(int(f2br), int(f2br0))
        self.assertIn("Николай Звягинцев", text)
        self.assertIn("Вероника А.", text)
        self.assertIn("25 473", text)
        reg, _ = sbp._gids_per_font_in_stream(cs)
        empty = [
            cid for cid in reg
            if int(cid) not in (0, 3) and not sbp._cid_has_contour(ff1, int(cid))
        ]
        self.assertEqual(empty, [])

    def test_orphan_and_cmap72_on_live_fail16(self) -> None:
        import fitz
        import tbank_unlock_template as tut

        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (16).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (16) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        out = sbp._sbp_prune_f1_tounicode_to_used(pdf)
        out = sbp._sbp_cap_f1_orphans_and_cmap_pdf(out)
        out = sbp._sbp_clamp_f1_cmap_band_pdf(out)
        doc = fitz.open(stream=out, filetype="pdf")
        fr = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        ff = doc.xref_stream(int(fr["fontfile_xref"]))
        tu = tut._parse_subset_tounicode(
            doc.xref_stream(int(fr["tounicode_xref"])).decode(
                "latin1", "replace"
            )
        )
        cs = doc.xref_stream(doc[0].get_contents()[0])
        text = doc[0].get_text()
        g = sbp._glyf_table_length(ff)
        cmap_n = len(tu)
        reg, _ = sbp._gids_per_font_in_stream(cs)
        used = set(int(c) for c in reg) | {0, 3} | set(int(c) for c in tu.keys())
        clo = sbp._ff2_raw_composite_closure(ff, used)
        orph = sorted(
            int(x) for x in sbp._ff2_nonempty_gids(ff)
            if int(x) not in clo and int(x) not in (0, 35, 239)
        )
        empty = [
            cid for cid in reg
            if int(cid) not in (0, 3) and not sbp._cid_has_contour(ff, int(cid))
        ]
        band = sbp._f1_cmap_proton_band(cmap_n)
        doc.close()
        self.assertIsNotNone(band)
        self.assertGreaterEqual(g, band[0])
        self.assertLessEqual(g, band[1])
        self.assertEqual(orph, [])
        self.assertEqual(empty, [])
        self.assertIn("Михаил Киселев", text)
        self.assertIn("Валерия Г.", text)
        self.assertIn("30 764", text)

    def test_structure_gate_clears_live_fail16(self) -> None:
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (16).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (16) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        dirty = sbp._sbp_structure_violations(pdf)
        self.assertTrue(dirty)
        out = sbp._sbp_enforce_structure_gate(pdf)
        self.assertFalse(sbp._sbp_structure_violations(out))
        src = inspect.getsource(sbp._sbp_finalize_for_ship)
        self.assertIn("_sbp_enforce_structure_gate", src)

    def test_corrupt_phi_detected_and_not_trimmed(self) -> None:
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (27).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live meridian PDF (27) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        empty = sbp._pdf_f1_empty_painted_chars(pdf)
        self.assertIn("Ф", empty)
        out = sbp._sbp_fill_empty_painted_pdf(pdf)
        empty2 = sbp._pdf_f1_empty_painted_chars(out)
        self.assertNotIn("Ф", empty2)
        import fitz
        import tbank_unlock_template as tut
        doc = fitz.open(stream=out, filetype="pdf")
        fr = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        ff = doc.xref_stream(int(fr["fontfile_xref"]))
        doc.close()
        self.assertTrue(sbp._cid_has_contour(ff, 256))
        self.assertGreaterEqual(sbp._glyph_compiled_len(ff, 256), 400)


    def test_cmap_retarget_live_fail34(self) -> None:
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (34).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (34) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        flags = sbp._sbp_structure_violations(pdf)
        self.assertTrue(any(f.startswith("cmap-outlier") for f in flags))
        out = sbp._sbp_enforce_structure_gate(pdf)
        flags2 = sbp._sbp_structure_violations(out)
        self.assertFalse(
            any(f.startswith("cmap-outlier") for f in flags2),
            flags2,
        )
        self.assertNotIn("Ф", sbp._pdf_f1_empty_painted_chars(out))
        import fitz
        text = fitz.open(stream=out, filetype="pdf")[0].get_text()
        self.assertIn("Финанс", text)


    def test_orphan_residue_cleared_on_live_fail37(self) -> None:
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (37).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (37) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        self.assertIn("Ф", sbp._pdf_f1_empty_painted_chars(pdf))
        out = sbp._sbp_enforce_structure_gate(pdf)
        flags = sbp._sbp_structure_violations(out)
        self.assertFalse(
            any(f.startswith("f1-orphan") or f.startswith("scan:") for f in flags),
            flags,
        )
        self.assertNotIn("Ф", sbp._pdf_f1_empty_painted_chars(out))
        import fitz
        import tbank_unlock_template as tut
        doc = fitz.open(stream=out, filetype="pdf")
        fr = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        ff = doc.xref_stream(int(fr["fontfile_xref"]))
        tu = tut._parse_subset_tounicode(
            doc.xref_stream(int(fr["tounicode_xref"])).decode("latin1", "replace")
        )
        cs = doc.xref_stream(doc[0].get_contents()[0])
        text = doc[0].get_text()
        doc.close()
        reg, _ = sbp._gids_per_font_in_stream(cs)
        orph = sbp._f1_disallowed_orphan_gids(
            ff, set(int(c) for c in reg) | {0, 3} | set(tu.keys()),
        )
        self.assertEqual(orph, [])
        self.assertTrue(sbp._cid_has_contour(ff, 256))
        self.assertIn("Премьер Финанс", text)
        self.assertIn("Руслан Власенко", text)
        g = sbp._glyf_table_length(ff)
        band = sbp._f1_cmap_proton_band(len(tu))
        self.assertIsNotNone(band)
        self.assertGreaterEqual(g, band[0])
        self.assertLessEqual(g, band[1])

    def test_fio_token_exact_rejects_glued_pad(self) -> None:
        self.assertTrue(
            sbp._fio_token_exact("Илья Мартынов\nОтправитель", "Илья Мартынов")
        )
        self.assertFalse(
            sbp._fio_token_exact("Илья МартыновИл\nОтправитель", "Илья Мартынов")
        )
        self.assertIsNone(
            sbp._equal_len_face(
                "Илья Мартынов",
                "Дамир Сеничев!!",
                allow_letter_pad=False,
            )
        )

    def test_face_rejects_glued_sender_on_live_fail40(self) -> None:
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (40).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (40) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        prepared = {
            "_user_sender": "Илья Мартынов",
            "sender": "Илья Мартынов",
            "_user_receiver": "София Г.",
            "receiver": "София Г.",
            "_user_bank": "Зенит Финанс",
            "bank": "Зенит Финанс",
            "new_amount": "8157",
            "phone": "+7 (900) 000-20-19",
        }
        ok, why = sbp._pdf_face_has_user_fields(
            pdf, prepared, allow_fio_drift=False,
        )
        self.assertFalse(ok, "glued «МартыновИл» must fail face gate")
        self.assertIn("sender", why)
        forced = sbp._sbp_force_correct_face(pdf, prepared)
        self.assertIsNotNone(forced)
        out = sbp._sbp_enforce_structure_gate(forced)
        ok2, why2 = sbp._pdf_face_has_user_fields(
            out, prepared, allow_fio_drift=False,
        )
        self.assertTrue(ok2, why2)
        import fitz
        text = fitz.open(stream=out, filetype="pdf")[0].get_text()
        self.assertIn("Илья Мартынов", text)
        self.assertNotIn("МартыновИл", text)
        self.assertIn("Зенит Финанс", text)
        self.assertNotIn("Ф", sbp._pdf_f1_empty_painted_chars(out))
        flags = sbp._sbp_structure_violations(out)
        self.assertFalse(
            any(
                f.startswith("cmap-outlier") or f.startswith("f1-orphan")
                for f in flags
            ),
            flags,
        )

    def test_cs_flate_tail_and_composites_on_live_fail47(self) -> None:
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (47).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (47) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        flags0 = sbp._sbp_structure_violations(pdf)
        self.assertTrue(
            any(f.startswith("cs-flate-tail") for f in flags0), flags0,
        )
        self.assertTrue(
            any(f.startswith("f1-transplant") for f in flags0), flags0,
        )
        self.assertGreater(sbp._sbp_cs_flate_unused_len(pdf), 0)
        out = sbp._sbp_enforce_structure_gate(pdf)
        flags = sbp._sbp_structure_violations(out)
        self.assertFalse(
            any(
                f.startswith("cs-flate-tail") or f.startswith("f1-transplant")
                for f in flags
            ),
            flags,
        )
        self.assertEqual(sbp._sbp_cs_flate_unused_len(out), 0)
        from io import BytesIO
        from fontTools.ttLib import TTFont
        import fitz
        import tbank_unlock_template as tut
        doc = fitz.open(stream=out, filetype="pdf")
        ff = doc.xref_stream(
            tut._find_font_objects(doc)["TinkoffSans-Regular"]["fontfile_xref"]
        )
        text = doc[0].get_text()
        doc.close()
        ft = TTFont(BytesIO(ff))
        g = ft["glyf"][ft.getGlyphOrder()[285]]
        try:
            g.expand(ft["glyf"])
        except Exception:
            pass
        _aw, lsb = ft["hmtx"].metrics[ft.getGlyphOrder()[285]]
        self.assertLess(int(g.numberOfContours), 0)
        self.assertEqual(int(lsb), int(g.xMin))
        self.assertIn("Аркадий Федосеев", text)
        self.assertIn("Рубеж Капитал", text)

    def test_raw_glyph_e_prefers_composite(self) -> None:
        entry = sbp._raw_glyph_entry(ord("е"), 273, is_medium=False)
        self.assertIsNotNone(entry)
        raw = bytes(entry["raw"])
        self.assertGreaterEqual(len(raw), 10)
        self.assertLess(int.from_bytes(raw[:2], "big", signed=True), 0)
        self.assertIn(138, entry.get("comps") or {})

    def test_atlas_composites_restored_on_live_fail46(self) -> None:
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (46).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (46) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        flags0 = sbp._sbp_structure_violations(pdf)
        self.assertTrue(any(f.startswith("f1-transplant") for f in flags0), flags0)
        out = sbp._sbp_enforce_structure_gate(pdf)
        self.assertFalse(
            any(f.startswith("f1-transplant") for f in sbp._sbp_structure_violations(out))
        )
        from io import BytesIO
        from fontTools.ttLib import TTFont
        import fitz
        import tbank_unlock_template as tut
        doc = fitz.open(stream=out, filetype="pdf")
        ff = doc.xref_stream(
            tut._find_font_objects(doc)["TinkoffSans-Regular"]["fontfile_xref"]
        )
        text = doc[0].get_text()
        doc.close()
        ft = TTFont(BytesIO(ff))
        g = ft["glyf"][ft.getGlyphOrder()[285]]
        try:
            g.expand(ft["glyf"])
        except Exception:
            pass
        _aw, lsb = ft["hmtx"].metrics[ft.getGlyphOrder()[285]]
        self.assertLess(int(g.numberOfContours), 0)
        self.assertEqual(int(lsb), int(g.xMin))
        self.assertIn("Константин Белов", text)
        self.assertIn("Континент Банк", text)

    def test_atlas_composites_restored_on_live_fail45(self) -> None:
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_29.08.2026 (45).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live FAIL PDF (45) not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        import fitz
        import tbank_unlock_template as tut
        from io import BytesIO
        from fontTools.ttLib import TTFont

        doc0 = fitz.open(stream=pdf, filetype="pdf")
        fr0 = tut._find_font_objects(doc0)["TinkoffSans-Regular"]
        ff0 = doc0.xref_stream(int(fr0["fontfile_xref"]))
        doc0.close()
        ft0 = TTFont(BytesIO(ff0))
        g285 = ft0["glyf"][ft0.getGlyphOrder()[285]]
        self.assertGreater(int(getattr(g285, "numberOfContours", 0) or 0), 0)
        flags0 = sbp._sbp_structure_violations(pdf)
        self.assertTrue(
            any(f.startswith("f1-transplant") for f in flags0),
            flags0,
        )

        out = sbp._sbp_enforce_structure_gate(pdf)
        flags = sbp._sbp_structure_violations(out)
        self.assertFalse(
            any(
                f.startswith("cmap-outlier") or f.startswith("f1-orphan")
                for f in flags
            ),
            flags,
        )
        doc = fitz.open(stream=out, filetype="pdf")
        fr = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        ff = doc.xref_stream(int(fr["fontfile_xref"]))
        tu = tut._parse_subset_tounicode(
            doc.xref_stream(int(fr["tounicode_xref"])).decode("latin1", "replace")
        )
        cs = doc.xref_stream(doc[0].get_contents()[0])
        text = doc[0].get_text()
        doc.close()
        ft = TTFont(BytesIO(ff))
        go = ft.getGlyphOrder()
        hmtx = ft["hmtx"].metrics
        for gid, ch in ((273, "е"), (285, "р")):
            g = ft["glyf"][go[gid]]
            try:
                g.expand(ft["glyf"])
            except Exception:
                pass
            self.assertLess(
                int(getattr(g, "numberOfContours", 0) or 0),
                0,
                f"{ch}@{gid} must stay a 16-byte composite",
            )
            aw, lsb = hmtx[go[gid]]
            self.assertEqual(int(lsb), int(g.xMin), f"{ch} LSB≠xMin")
            entry = sbp._raw_glyph_entry(ord(ch), gid, is_medium=False)
            offs, glyf_b, _, _ = __import__("tbank_dynamic")._f1_loca_tables(ff)
            cur = bytes(glyf_b[offs[gid]:offs[gid + 1]])
            self.assertEqual(cur, bytes(entry["raw"]), f"{ch} mosaic raw")
        self.assertIn("Максим Нестеров", text)
        self.assertIn("Панорама Финанс", text)
        self.assertNotIn("Ф", sbp._pdf_f1_empty_painted_chars(out))
        reg, _ = sbp._gids_per_font_in_stream(cs)
        orph = sbp._f1_disallowed_orphan_gids(
            ff, set(int(c) for c in reg) | {0, 3} | set(tu.keys()),
        )
        self.assertEqual(orph, [])
        g = sbp._glyf_table_length(ff)
        band = sbp._f1_cmap_proton_band(len(tu))
        self.assertIsNotNone(band)
        self.assertGreaterEqual(g, band[0])
        self.assertLessEqual(g, band[1])

    def test_card_sber_fills_ys_and_retags_donor_prefix(self) -> None:
        import hashlib

        import fitz
        import tbank_unlock_template as tut

        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_30.08.2026.pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live card_sber FAIL PDF not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        self.assertIn("ы", sbp._pdf_f1_empty_painted_chars(pdf))
        filled = sbp._sbp_fill_empty_painted_pdf(pdf, clamp_cmap_band=False)
        self.assertNotIn("ы", sbp._pdf_f1_empty_painted_chars(filled))
        tagged = sbp._randomize_pdf_fingerprints(filled)
        self.assertNotIn("ы", sbp._pdf_f1_empty_painted_chars(tagged))
        doc0 = fitz.open(stream=filled, filetype="pdf")
        fonts0 = tut._find_font_objects(doc0)
        sha0 = {
            name: hashlib.sha256(doc0.xref_stream(meta["fontfile_xref"])).digest()
            for name, meta in fonts0.items()
        }
        doc0.close()
        doc1 = fitz.open(stream=tagged, filetype="pdf")
        burned = {b"DYFYRM", b"ZAKDCZ"}
        corpus = tbank_dynamic.corpus_tbank_subset_tags()
        for name, meta in tut._find_font_objects(doc1).items():
            obj = doc1.xref_object(int(meta["type0_xref"]))
            m = re.search(r"/([A-Z]{6})\+(?:TinkoffSans|ALSRubl)", obj)
            self.assertIsNotNone(m, name)
            tag = m.group(1).encode("ascii")
            self.assertNotIn(tag, burned)
            self.assertNotIn(tag, corpus)
            self.assertEqual(
                hashlib.sha256(doc1.xref_stream(meta["fontfile_xref"])).digest(),
                sha0[name],
                msg=f"FontFile2 mutated during retag {name}",
            )
        doc1.close()

    def test_card_sber_pipeline_retags_after_ff2(self) -> None:
        src = inspect.getsource(tbank_dynamic.create_tbank_pipeline)
        self.assertIn("_tbank_finish_non_sbp_ship", src)
        self.assertIn("card_sber", src)
        finish = inspect.getsource(tbank_dynamic._tbank_finish_non_sbp_ship)
        self.assertIn("_sbp_finalize_for_ship", finish)
        self.assertIn("_tbank_force_correct_face", finish)
        fin_src = inspect.getsource(sbp._sbp_finalize_for_ship)
        self.assertIn("_sbp_enforce_structure_gate", fin_src)
        self.assertGreater(
            fin_src.rfind("_sbp_enforce_structure_gate"),
            fin_src.rfind("_randomize_pdf_fingerprints"),
        )
        self.assertNotIn("keep font prefixes", src)

    def test_card_sber_peel_lands_proton_band_and_retags(self) -> None:
        import hashlib

        import fitz
        import tbank_unlock_template as tut

        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_30.08.2026 (2).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live card_sber FAIL (2) PDF not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        peeled = tbank_dynamic._peel_f1_glyf_to_cmap_band(pdf, height=471)
        self.assertIsNotNone(peeled)
        tagged = sbp._randomize_pdf_fingerprints(peeled)
        doc = fitz.open(stream=tagged, filetype="pdf")
        meta = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        ff = doc.xref_stream(meta["fontfile_xref"])
        tu = tut._parse_subset_tounicode(
            doc.xref_stream(meta["tounicode_xref"]).decode("latin1", "replace")
        )
        obj = doc.xref_object(int(meta["type0_xref"]))
        m = re.search(r"/([A-Z]{6})\+(?:TinkoffSans|ALSRubl)", obj)
        self.assertIsNotNone(m)
        tag = m.group(1).encode("ascii")
        glyf = sbp._glyf_table_length(ff)
        cmap_n = len(tu)
        text = doc[0].get_text()
        doc.close()
        bands = {61: (11614, 11620), 62: (11302, 11342), 63: (11732, 11732)}
        lo, hi = bands[cmap_n]
        self.assertGreaterEqual(glyf, lo - 400)
        self.assertLessEqual(glyf, hi + 400)
        self.assertNotIn(tag, {b"DYFYRM", b"ZAKDCZ"})
        self.assertNotIn(tag, tbank_dynamic.corpus_tbank_subset_tags())
        self.assertIn("По номеру карты", text)
        self.assertIn("Вектор Банк", text)
        self.assertNotIn("ы", sbp._pdf_f1_empty_painted_chars(tagged))
        sha = hashlib.sha256(ff).hexdigest()[:16]
        self.assertNotEqual(sha, "0f071b261aaa4ffe")

    def test_card_sber_finish_lands_cmap60_oversize(self) -> None:
        import fitz
        import tbank_unlock_template as tut

        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_30.08.2026 (3).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live card_sber FAIL (3) PDF not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        out = tbank_dynamic._tbank_finish_non_sbp_ship(
            pdf, height=471, channel="card_sber",
        )
        doc = fitz.open(stream=out, filetype="pdf")
        meta = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        ff = doc.xref_stream(meta["fontfile_xref"])
        tu = tut._parse_subset_tounicode(
            doc.xref_stream(meta["tounicode_xref"]).decode("latin1", "replace")
        )
        obj = doc.xref_object(int(meta["type0_xref"]))
        m = re.search(r"/([A-Z]{6})\+(?:TinkoffSans|ALSRubl)", obj)
        tag = m.group(1).encode("ascii")
        glyf = sbp._glyf_table_length(ff)
        cmap_n = len(tu)
        text = doc[0].get_text()
        doc.close()
        bands = {60: (11142, 11142), 61: (11614, 11620), 62: (11302, 11342), 63: (11732, 11732)}
        lo, hi = bands[cmap_n]
        self.assertGreaterEqual(glyf, lo - 400)
        self.assertLessEqual(glyf, hi + 400)
        self.assertNotIn(tag, {b"DYFYRM", b"ZAKDCZ"})
        self.assertIn("Орбита Финанс", text)
        self.assertIn("По номеру карты", text)
        self.assertNotIn("ы", sbp._pdf_f1_empty_painted_chars(out))

    def test_card_sber_finish_retags_receipt4(self) -> None:
        import hashlib

        import fitz
        import tbank_unlock_template as tut

        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_30.08.2026 (4).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("live card_sber FAIL (4) PDF not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        doc0 = fitz.open(stream=pdf, filetype="pdf")
        sha0 = {
            name: hashlib.sha256(
                doc0.xref_stream(meta["fontfile_xref"])
            ).digest()
            for name, meta in tut._find_font_objects(doc0).items()
        }
        doc0.close()
        out = tbank_dynamic._tbank_finish_non_sbp_ship(
            pdf, height=471, channel="card_sber",
        )
        live = sbp._live_subset_tags(out)
        self.assertTrue(live)
        self.assertTrue(live.isdisjoint({b"DYFYRM", b"ZAKDCZ"}))
        self.assertTrue(live.isdisjoint(tbank_dynamic.corpus_tbank_subset_tags()))
        doc1 = fitz.open(stream=out, filetype="pdf")
        if sbp._h471_f1_exact_pdf(pdf):
            for name, meta in tut._find_font_objects(doc1).items():
                self.assertEqual(
                    hashlib.sha256(doc1.xref_stream(meta["fontfile_xref"])).digest(),
                    sha0[name],
                    msg=f"FontFile2 mutated during finish retag {name}",
                )
        text = doc1[0].get_text()
        doc1.close()
        self.assertIn("Каскад Банк", text)
        self.assertIn("Владислав Тарасов", text)

    def test_randomize_never_writes_incremental_xref(self) -> None:
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_30.08.2026 (5).pdf",
        )
        if not os.path.isfile(path):
            path = os.path.join(ROOT, "templates", "T_phone_original.pdf")
        with open(path, "rb") as fh:
            pdf = fh.read()
        out = sbp._randomize_pdf_fingerprints(pdf)
        self.assertEqual(out.count(b"startxref"), 1)
        self.assertEqual(out.count(b"%%EOF"), 1)
        self.assertTrue(sbp._live_subset_tags(out))
        self.assertTrue(
            sbp._live_subset_tags(out).isdisjoint({b"DYFYRM", b"ZAKDCZ"})
        )

    def test_dynamic_enc_space_is_cid_3(self) -> None:
        self.assertEqual(sbp._dynamic_enc(" ", {0x20: 239, 0x30: 305}), b"\x00\x03")
        self.assertEqual(
            sbp._dynamic_enc("0 ", {0x20: 239, 0x30: 305}),
            b"\x01\x31\x00\x03",
        )

    def test_orig_text_width_space_is_cid_3(self) -> None:
        from tbank_orig_mode import OrigContext

        ctx = OrigContext()
        self.assertTrue(ctx.load(phone.PHONE_ORIG_NARROW))
        w_space = ctx.text_width(" ", 9.0, medium=False)
        w_cid3 = ctx.width_reg.get(3, ctx.dw_reg) * 9.0 / 1000.0
        self.assertAlmostEqual(w_space, w_cid3, places=5)

    def test_dynamic_replace_tj_ignores_empty_old(self) -> None:
        stream = b"BT\n1 0 0 1 0 451 Tm\n()Tj\nET\n"
        out, ok = sbp._dynamic_replace_tj(
            stream, b"", b"\x00\xeb", "", "X", 9.0, None, {},
        )
        self.assertFalse(ok)
        self.assertEqual(out, stream)

    def test_phone_restore_stamp_clears_date_line_junk(self) -> None:
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_30.08.2026 (6).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("receipt (6).pdf not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        import fitz

        doc = fitz.open(stream=pdf, filetype="pdf")
        first0 = (doc[0].get_text().splitlines() or [""])[0].strip()
        doc.close()
        self.assertFalse(
            re.match(r"^\d{2}\.\d{2}\.\d{4}", first0),
            msg="fixture should still show the DATE_LINE junk",
        )
        out = phone._phone_restore_stamp_in_pdf(pdf)
        doc = fitz.open(stream=out, filetype="pdf")
        first = (doc[0].get_text().splitlines() or [""])[0].strip()
        doc.close()
        self.assertRegex(first, r"^\d{2}\.\d{2}\.\d{4}")

    def test_phone_receipt7_peel_lands_exact_glyf(self) -> None:
        """(7).pdf glyf=11620 / cmap=62 is an atlas gap → OnlyPDF «не распознан»."""
        path = os.path.join(
            os.path.expanduser("~"),
            "Downloads",
            "Telegram Desktop",
            "receipt_30.08.2026 (7).pdf",
        )
        if not os.path.isfile(path):
            self.skipTest("receipt (7).pdf not on disk")
        with open(path, "rb") as fh:
            pdf = fh.read()
        import fitz
        import tbank_unlock_template as tut

        out = tbank_dynamic._peel_f1_glyf_to_cmap_band(pdf, height=451)
        self.assertIsNotNone(out)
        assert out is not None
        doc = fitz.open(stream=out, filetype="pdf")
        meta = tut._find_font_objects(doc)["TinkoffSans-Regular"]
        ff2 = doc.xref_stream(meta["fontfile_xref"])
        tu = tut._parse_subset_tounicode(
            doc.xref_stream(meta["tounicode_xref"]).decode("latin1", "replace")
        )
        text = doc[0].get_text()
        doc.close()
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        self.assertRegex(first, r"^\d{2}\.\d{2}\.\d{4}")
        g = sbp._glyf_table_length(ff2)
        self.assertEqual(len(tu), 62)
        self.assertIn(g, (11366, 11516, 11538, 11576, 11626))

    def test_phone_che_receiver_and_date_first_line(self) -> None:
        pdf = phone.create_tbank_phone_stealth(
            {
                "date_time": "12.06.2026 14:22:33",
                "amount": "18400",
                "sender": "Пётр Николаев",
                "receiver": "Марина Ч.",
                "phone": "+7 (901) 234-56-78",
                "receipt_num": "авто",
            }
        )
        self.assertIsNotNone(pdf)
        assert pdf is not None
        import fitz
        import tbank_unlock_template as tut

        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        self.assertRegex(first, r"^12\.06\.2026")
        self.assertIn("Марина Ч.", text)
        self.assertNotIn("Ă", text)
        ruble_x1 = []
        for b in doc[0].get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b.get("lines", []):
                for s in line.get("spans", []):
                    if s.get("text", "").strip() in ("₽", "i") and s["bbox"][2] > 200:
                        ruble_x1.append(s["bbox"][2])
        tu = tut._parse_subset_tounicode(
            doc.xref_stream(
                tut._find_font_objects(doc)["TinkoffSans-Regular"]["tounicode_xref"]
            ).decode("latin1", "replace")
        )
        doc.close()
        self.assertEqual(tu.get(258), 0x427)
        self.assertTrue(ruble_x1)
        for x1 in ruble_x1:
            self.assertLessEqual(abs(x1 - 250.0), 1.8, msg=f"ruble x1={x1}")


if __name__ == "__main__":
    unittest.main()
