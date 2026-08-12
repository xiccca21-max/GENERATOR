# OnlyPDF-verified generation — **one path per channel**

## Canonical rule (do not break)

1. **Live generator** = one `create_*_stealth` function (used by `bot.py`).
2. **QA / examples** = only `tools/gen_onlypdf30_*.py` → `tools/onlypdf_gate.py`
   (`@onlypdf_robot` PASS×2 + full recheck).
3. **Never** add ungated “demo / all15 / all25 / quick folder” scripts that call
   `create_*` without the gate and ship those PDFs as “examples”.
4. Channel without 30/30 in the table below = **blocked** (no bot button, no examples).

```text
python tools/gen_onlypdf30_sber_sbp.py -n 5 --out output/sber_sbp_onlypdf
python tools/gen_onlypdf30_sber_phone.py -n 5 --out output/sber_phone_onlypdf
```

Gate: `tools/onlypdf_gate.py`. Names: `tools/onlypdf_safe_names.py` (no ё / ъ).

## Status 2026-07-23 (full suite)

Master: `tools/run_all_onlypdf30.py` → `output/onlypdf_full_run/summary.json` (**10/10** OnlyPDF).

Coverage re-gate (alphabet + digits): `tools/run_coverage_fix.py` →
`output/onlypdf_full_run/coverage_fix_summary.json` (**ok**, 2026-07-24):
T-Bank SBP х/э closed; Sber SBP/phone `щ` closed (font-patch + skeleton TJ escape fix).
All three folders: cyr_miss=нет, dig_miss=нет, OnlyPDF 30/30.

**Live UI banks** (`bot.py` `LIVE_BANKS`): Sber, Alfa, T-Bank.
VTB / OTP / Ozon / Sber card / T-Bank statement — hidden until OnlyPDF 30/30.
Alfa was briefly hidden (2026-07-26) due to OnlyPDF `SERVICE_ERROR` on originals;
re-enabled 2026-07-29 after strict50 `50/50 FAKE=0` on SBP/card/phone.

### T-Bank — all channels 30/30 PASS
| Channel | Generator | Batch |
|---------|-----------|-------|
| SBP | `create_tbank_sbp_stealth` | `gen_onlypdf30.py` (force х/э names) |
| Card→Sber | `create_tbank_stealth` (card sber) | `gen_onlypdf30_card_sber.py` |
| Card→T-Bank | `create_tbank_card_tbank_stealth` | `gen_onlypdf30_card_tbank.py` |
| Phone | `create_tbank_phone_stealth` | `gen_onlypdf30_phone.py` |
| Nocomm | `create_tbank_nocomm_stealth` | `gen_onlypdf30_nocomm.py` |

### Alfa — all channels 30/30 PASS
| Channel | Generator | Batch | Coverage |
|---------|-----------|-------|----------|
| SBP | `create_alfa_sbp_stealth` | `gen_onlypdf30_alfa_sbp.py` | full cyr + digits |
| Card | `create_alfa_card_stealth` | `gen_onlypdf30_alfa_card.py` | **digits only** (no FIO; label gaps OK) |
| Phone | `create_alfa_phone_stealth` | `gen_onlypdf30_alfa_phone.py` | full cyr + digits |

### Sber
| Channel | Generator | OnlyPDF | Notes |
|---------|-----------|---------|-------|
| SBP | `create_sber_sbp_stealth` | **30/30** | `щ` via font-patch; skeleton TJ escape fix |
| Phone | `create_sber_phone_stealth` | **30/30** | same `щ` path |
| Card→other | — | **blocked** | Donor flagged virtual printer even as original. Bot button removed until clean donor + 30/30. |

## Sber `щ` + skeleton hash (2026-07-23)

- No donor cmap has `щ`/`Щ` → content-only soft-fit used to drop surnames.
- Fix: if text contains `щ`/`Щ`, skip content-only; force font-patch on wide name slots;
  soft-fit must keep protected `щ`.
- Skeleton hash bug: naive `\([^)]*\)Tj` broke when CID bytes escaped as `\)` —
  use `_mask_pdf_literal_tj()` so font-patch can grow tables with new glyphs.
- Removed `щ→ш` from `SHELL_CHAR_FALLBACK` in `sber_glyph_library.py`.

## Structural hardening (SafeCheck)

SafeCheck «структура» while OnlyPDF passes: usually post-hoc F1 FontFile2 Length rewrite.
OnlyPDF gate remains the acceptance loop for shipping PDFs.
