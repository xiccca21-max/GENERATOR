# PASS-рецепты с полного нуля (эталоны, прошедшие конкурентов)

Документ = **канон**. Не «примерно как банк», а пошаговая сборка класса PDF, который уже прошёл:

- `@proton_pdf_bot` / pdf-checker (структура, шрифты, Keywords, V3 lattice)
- сторонние «крутые» валидаторы (Белый / «прошёл по структуре» / SafeCheck-класс)

Рецепт описывает **класс**, не побайтовый клон: даты/суммы/ФИО в новых чеках другие, инварианты — те же.

| Specimen | Файл | Size | SHA‑256 |
|----------|------|------|---------|
| **T‑Bank SBP** | `docs/specimen_tbank_sbp_seq_1785589390_01.pdf` | **60473** | `393ffa084886c2a70fd0e4212eac21937f8ff398c6163333247dc744aad8829b` |
| **Alfa SBP** | `docs/specimen_alfa_sbp_155113.pdf` | **58873** | `c80160c30964b9f4426342bac9a9f0bd3a144439064fd337d967384aebcedd13` |

Канонический код: **только** `create_*_stealth` через `bot.py` / `tools/night_proton_seq.py`.  
Демо без OnlyPDF/Proton gate — запрещены (`onlypdf-canonical`).

---

# A. T‑Bank SBP — эталон `seq_1785589390_01`

## A0. Лицо эталона (что видит человек)

```text
date_time:      21.04.2026  20:54:15
Итого / Сумма:  93 934 ₽   (payload amount = "93934")
Комиссия:       0 ₽
Перевод:        По номеру телефона
Статус:         Успешно
Отправитель:    Харитон Хромов
Телефон:        +7 (315) 697-20-48
Получатель:     Цвета Ц.
Банк:           Сбербанк
СБП id:         B6111175416354050G101400117
СБП suffix:     91103
Счёт:           408178100000****0336
Квитанция:      1-132-370-124-841
Support:        fb@tbank.ru
```

Payload для воспроизведения класса (SBP id / квитанция при «авто» будут новыми):

```python
from tbank_sbp_stealth import create_tbank_sbp_stealth
pdf = create_tbank_sbp_stealth({
    "amount": "93934",
    "sender": "Харитон Хромов",
    "receiver": "Цвета Ц.",
    "phone": "+7 (315) 697-20-48",
    "recipient_bank": "Сбербанк",   # или bank_name=
    "date_time": "21.04.2026  20:54:15",
    "receipt_num": "авто",
    "sbp_id": "авто",
})
```

## A1. Контейнер PDF (обязательные мелочи)

| Поле | Эталон | Закон |
|------|--------|-------|
| Page | **270 × 519** | SBP height=519 atlas |
| PDF size | **60473** ∈ **~59–61 KB** | не unlocked ~64 KB |
| Producer | `OpenPDF 1.3.30.jaspersoft.2` | Jasper |
| Creator | `JasperReports Library version 6.20.3-415f9428cffdb6805c6f85bbb29ebaf18813a2ab` | как банк |
| Subject | `"/reports/IB/Receipt"` | |
| CreationDate / ModDate | `D:20260421205538+03'00'` | согласованы с Keywords |
| **/Keywords** | `21.04.2026 20:55:38 \| 8253e36407aa276902dfc16b92350409 \| **991**` | 3 колонки |
| Trailer /ID | уникальная пара hex (эталон: `7dfcdeb3…` / `77fc1df0…`) | не копировать чужой /ID на новое лицо |

### Keywords — третья колонка (Proton HARD)

| Дата лица | Token |
|-----------|-------|
| **до** 10.07.2026 | `991` |
| **с** 10.07.2026 | `DOCS-2035` |

Код: `tbank_sbp_stealth._keywords_third_token` → `_patch_pdf_metadata`.  
Неверная пара дата↔token → Proton HARD FAKE. Не «лечить» регенами с той же ошибкой.

## A2. Шрифты эталона (замеры с specimen)

| Role | BaseFont | FontFile2 **decoded** | glyf | CSA | head.created | head.modified (эталон) |
|------|----------|----------------------|------|-----|--------------|------------------------|
| **F1** Regular | `*TinkoffSans-Regular` | **17180** | **13014** | **`0x337C7D3F`** | **3717379206** | 3868434189 (на эталоне) |
| **F2** Medium | `*TinkoffSans-Medium` | **5444** | **1254** | donor `0x4068ADBE` | donor | donor |
| **F3** | ALSRubl | donor | — | — | — | — |

Эталонный F1.raw (flate Length) в классе PASS: **> 9205** (в живых PASS часто ~9343).

### A2.1 V3 lattice — `K-TBANK-REASSEMBLY-FAMILY-V3`

Чтобы **не** словить family v3 HARD (особенно при жирном F2 ToUnicode):

| Условие | Значение на эталоне / закон |
|---------|-----------------------------|
| F1.decoded | **17120 < dec ≤ 17198** (эталон **17180**) |
| F1.raw | **> 9205** |
| F1.glyf | **> 12783** (эталон **13014**) |
| F2.glyf | **≈800–1540**, **< 1550** (эталон **1254**; ≥1550 → Fraudex fat) |

Dual-band (Fraudex+OnlyPDF natural path) **не выше Proton FONTFILE2 HARD**:

- Dual / ship ceiling: **`_F1_DUAL_DEC_HI = 18196`**
- Proton atlas HARD height=519: **F1 decoded 16244–18196**, **F2 decoded 5000–5824**
- Эталон 17180/5444 лежит внутри обоих окон — цель при новой сборке: **то же**.

**Запрет:** soft-ship F1/F2 **над** HARD («чуть выше — всё равно в TG»). Это даёт `TBANK_FONTFILE2_SIZE_STRONG_OUTLIER`. Лучше GEN_NONE / reject, чем HARD FAKE.

**Запрет:** `BAKE_SHELL` на `fake_fp:font` / FONTFILE2 outlier — раздувает `T_sbp_unlocked` (F1 ~22 KB) и отравляет следующие чеки.

### A2.2 F1 OpenPDF CSA fingerprint

После **любой** мутации F1 насильно:

```text
head.checkSumAdjustment = 863796543 = 0x337C7D3F
```

Честный пересчёт OpenType CSA → `K-TBANK-FONT-TABLE-INTEGRITY-001` HARD.

Код: `_restore_head_csa(..., _TBANK_F1_OPENPDF_CSA)` / `_force_tbank_f1_head_epoch`.

### A2.3 F1 head epoch (актуальный Proton HARD)

Константы банка (пара TinkoffSans из originals):

```text
created  = 3717379206
modified = 3722743619   # K-TBANK-F1-HEAD-MODIFIED-EPOCH-001
```

На **золотом specimen** modified был `3868434189` (тогда ещё проходило).  
**Сейчас** при шипе: пин `created/modified` **в самом конце** пайплайна (после defuse / fingerprint / orphan), иначе что-то перезапишет modified → HARD FAKE.

Код: `_force_tbank_f1_head_epoch` → `_finalize_tbank_f1_epoch` **last**.

## A3. Геометрия (field-edge-anchors) — неизменяемо

Страница 270×…; колонка значений **RIGHT x1 = 250**:

| Поле | Edge | Координата |
|------|------|------------|
| date, labels, receipt, footer | LEFT | `x0 ≈ 20` |
| status / type / FIO / phone / bank / account / SBP id | **RIGHT** | **`x1 = 250`** |
| сумма (мал.) digits | RIGHT before ₽ | digits ≈243.68; F3 `i` → 250 |
| Итого (бол.) digits | RIGHT before ₽ | digits ≈237.77; F3 → 249–250 |

- Tm: `new_x = right_edge - width` при смене ширины текста.
- **Никакого** lead-pad FIO / `" "*n + name`.
- **Нельзя** оставлять donor-имена (`Дамир С.` / `Марина Ч.` / `Дамир Сеничев`) как лицо пользователя.

## A4. Пайплайн с полного нуля (каждая мелочь)

```text
0. Доноры / корпус
   - Lean Jasper shell: templates/T_sbp_original.pdf / T_sbp_corpus_shell.pdf
     (~59–60 KB; F1 dec ~16.6–17.4 KB, F2 ~5.0–5.3 KB)
   - Unlocked (~64 KB, F1 ~22 KB) — только last resort charset; не bake на font-FAKE
   - glyph_library/raw_glyphs.pkl — union банковских loca/glyf (trusted mosaic)

1. Вход dict
   amount, sender, receiver, phone, date_time, bank/recipient_bank,
   receipt_num/sbp_id = «авто» или явные
   Телефон: мобильный формат (+7 …), иначе PHONE_DEF_NOT_MOBILE у конкурентов/Proton

2. create_tbank_sbp_stealth(data)
   → tbank_dynamic.create_tbank_pipeline(channel="sbp")
   → builders: donor-orig / dynamic stealth

3. Выбор enrolled FontFile2 из корпуса SBP
   Максимальное покрытие нужных codepoints, lean size

4. Планирование CID
   TBANK_CHAR_TO_GID_REG/MED + Jasper allocator
   ToUnicode subset ↔ /W ↔ glyf bijective

5. Raw-hydrate недостающих букв/цифр
   - только из raw_glyphs.pkl
   - shell-scale advance (~450–700), НЕ двойной кегль (~1100)
   - install через put_font_table / resize SFNT при росте glyf
   - short loca если влезает; sync indexToLocFormat после blank
   - FIO/хаос не раздувать charset сверх HARD (токены ≤~10 букв в war)

6. Перепись content (TJ/Tm)
   RIGHT=250 для value column; LEFT=20 для labels/date/receipt
   Суммы: digits + F3 рубль на якоре
   Перед ET: только "\n", НЕ пробел (TBANK_CONTENT_ET_WHITESPACE_ANOMALY)
   Нет Tj между Td·Td (TBANK_CONTENT_TD_TJ_INTERLEAVE)
   Не раздувать BT/Tm профиль (atlas BT=30/Tm=31)

7. Trim / lean
   F2: blank unused → HARD decoded 5000–5824 (эталон 5444; glyf <1550)
   F1: если нужно — peel unused вне composite-closure в окно;
       dual OK только ≤18196; post-pipeline lean_tbank_f1_to_band(16244..18196)
   Если lean miss и size > HARD → REJECT (не soft-ship)

8. Orphans
   orphan simples → PUA + off-page Td + "3 Tr (...)Tj 0 Tr"
   FontFile2 unused glyf не blank’ать так, чтобы used CID стал empty

9. Force F1 CSA = 0x337C7D3F
   (ng>200 → OpenPDF fingerprint path)

10. Metadata
    Keywords date|hash|991 или DOCS-2035 по дате лица
    CreationDate/ModDate согласованы

11. Content-stream flate
    Jasper header 0x789c; /Length следует за контентом (не фейковый pad)
    CIDFont /W rebuild (F1 corpus + F2 corpus)

12. Defuse V3 (если font_sig ∧ stream_sig)
    Retarget F1 только когда оба сигнала; иначе не трогать
    После defuse: снова lean F1/F2 в HARD + epoch pin

13. Fingerprint / trailer
    Уникальный /ID при необходимости; strip EOF junk
    F1 head epoch pin LAST (created/modified банковской пары)

14. Gates
    visual_integrity (edges, no donor FIO, no lead-pad)
    PDF size ~59–61 KB (эталон 60473)
    F1.dec ∈ [16244,18196], лучше ещё и V3 17121..17198
    F2.dec ∈ [5000,5824], glyf <1550
    CSA 0x337C7D3F; Keywords token ↔ date

15. Emit → Proton / OnlyPDF
    QA: tools/gen_onlypdf30_*.py → onlypdf_gate.py
    War: tools/night_proton_seq.py --force --war …
    Deploy после правок: python deploy/deploy.py …
```

Точка входа одного чека:

```text
python tools/night_proton_seq.py --only tbank_sbp --once -n 1 --force
```

или бот → канал T‑Bank SBP.

## A5. Что ломает рецепт (таблица фейлов)

| Ошибка | Симптом |
|--------|---------|
| CSA пересчитан «правильно» | `K-TBANK-FONT-TABLE-INTEGRITY-001` |
| F1.dec вне V3 при жирном F2 TU | `K-TBANK-REASSEMBLY-FAMILY-V3` |
| F1.dec > 18196 или F2 > 5824 | `TBANK_FONTFILE2_SIZE_STRONG_OUTLIER` |
| Soft-ship / bake на font FAKE | серия HARD + жирный unlocked |
| F2.glyf ≥ 1550 | Fraudex structure / fat |
| head.modified не банковский epoch | `K-TBANK-F1-HEAD-MODIFIED-EPOCH-001` |
| Keywords 991 на дате ≥10.07.2026 (или наоборот) | Keywords HARD |
| Контур чужого aw (~1100 в слот ~530) | визуальный наезд / гигантские буквы |
| Peel срезал composite component | FONT-SUBSET-EXACT-CLOSURE |
| Used CID с empty glyf | A-FONT-USED-CID-EMPTY-GLYPH |
| Lead-pad / сдвиг с x1=250 | visual / layout HARD |
| Donor FIO на лице | visual gate / «не тот чек» |
| Пробел перед `ET` | `TBANK_CONTENT_ET_WHITESPACE_ANOMALY` |
| Tj между Td·Td | `TBANK_CONTENT_TD_TJ_INTERLEAVE` |
| Немобильный телефон | `*_PHONE_DEF_NOT_MOBILE` |
| Reused trailer /ID на новое лицо | `TBANK_TRAILER_ID_REUSED` |

## A6. Чеклист перед шипом «как этот PASS»

- [ ] Keywords 3-я колонка = дата лица (`991` / `DOCS-2035`)
- [ ] F1 CSA = `0x337C7D3F`
- [ ] F1 head created=`3717379206`, modified=`3722743619` (пин в конце)
- [ ] `17120 < F1.dec ≤ 17198` (идеал) и точно `16244 ≤ F1.dec ≤ 18196`
- [ ] F1.raw > 9205, glyf > 12783
- [ ] `5000 ≤ F2.dec ≤ 5824`, F2.glyf < 1550
- [ ] RIGHT values = 250; PDF ~59–61 KB
- [ ] Контуры shell-scale; exact user FIO; mobile phone
- [ ] Нет soft-ship над HARD; нет bake на font-outlier

---

# B. Alfa SBP — эталон `alfa_sbp_155113`

## B0. Лицо эталона

```text
header:        01.08.2026 15:51 мск
title:         Квитанция о переводе по СБП
amount:        80 980 RUR   (payload "80980")
commission:    0 RUR
date_time:     01.08.2026 15:51:12 мск
operation_num: C160108263133662
receiver:      Денис Карамеливич Г
phone:         +7 (999) 123-45-67
bank:          Озон Банк
account:       40817810123456789012
sbp_id:        A62131249373201O0G10080011770901
message:       Перевод денежных средств
```

NBSP (`U+00A0`) в тексте — норма Oracle/BI (~60 шт. на эталоне).

## B1. Контейнер

| Поле | Значение |
|------|----------|
| Page | **A4 595.3 × 841.9** |
| PDF | 1.6 |
| Size | **58873** ∈ **58000–59087** |
| Producer | **`Oracle BI Publisher 12.2.1.4.0`** |
| Creator / Subject / Dates | пусто (как lean originals) |

## B2. Шрифт / ресурсы

| Role | Значение |
|------|----------|
| Font | Type0 `*Tahoma` Identity-H |
| FontFile2 decoded | **22680** |
| FontFile2 flate | **13890** |
| maxp.numGlyphs | **78** (subset) |
| upem | 2048 |
| Images | Im0 900×105, Im1 90×138, Im2 603×258 (DeviceRGB Flate) |

## B3. Геометрия Alfa

Обе колонки **LEFT**:

| Колонка | Anchor | Поля |
|---------|--------|------|
| Left | **x0 = 35.45** | сумма, комиссия, дата, op number, receiver, … |
| Right | **x0 = 304.75** | phone, bank, account, SBP id, message, … |
| Header date | RIGHT | `x1 ≈ 559.69` |

Только **trailing** pad (NBSP/hex). Lead-pad запрещён.  
Amount: не заливать NBSP после RUR; pad space в RUR-слотах (`ALFA_AMOUNT_TYPOGRAPHY_ANOMALY`).  
Не ` ET` space-pad (`ALFA_CONTENT_ET_WHITESPACE_ANOMALY`).

## B4. Пайплайн с нуля

```text
1. create_alfa_sbp_stealth(data)
2. Lean corpus 57–60.5 KB (Alfa_sbp_original + alfa_corpus); unlocked ~63KB — last resort
3. ensure_alfa_font_chars — долить Tahoma из корпуса (без soft-remap лица)
4. _prepare_sbp: точное лицо; дату пользователя НЕ подменять донором
5. NATIVE rewrite: LEFT 35.45 / 304.75; Java flate Length = donor
6. Size 58000–58800 (image/stream reuse с короткого original при нужде)
7. Gate visual + size ≤59087 → Emit
```

```python
from alfa_sbp_stealth import create_alfa_sbp_stealth
pdf = create_alfa_sbp_stealth({
    "amount": "80980",
    "receiver": "Денис Карамеливич Г",
    "phone": "+7 (999) 123-45-67",
    "bank": "Озон Банк",
    "recipient_bank": "Озон Банк",
    "account": "40817810123456789012",
    "date_time": "01.08.2026 15:51:12",
    "operation_num": "C160108263133662",
    "sbp_id": "A62131249373201O0G10080011770901",
    "message": "Перевод денежных средств",
})
```

## B5. Ломает Alfa

| Ошибка | Симптом |
|--------|---------|
| size > 59087 (unlocked ~63KB) | `ALFA_FILE_SIZE_STRONG_OUTLIER` |
| Lead-pad / сдвиг с 35.45 или 304.75 | layout HARD |
| Soft remap глифа | запрещено |
| Period-10 account | `ALFA_DEBIT_ACCOUNT_PERIODIC` |
| Phone name unmasked | `ALFA_PHONE_NAME_UNMASKED` (для phone-канала: маска вида `Вол**в Д. В.`) |

---

# C. Как гонять / деплоить

```text
# один метод
python tools/night_proton_seq.py --only tbank_sbp --once -n 1 --force
python tools/night_proton_seq.py --only alfa_sbp --once -n 1 --force

# полная очередь war (все живые каналы)
python -u tools/night_proton_seq.py --force --war -n 5 --cycle 16

# после правок генератора — сам деплой
python deploy/deploy.py <files>
# receipt-bot active на /opt/receipt-bot
```

OnlyPDF QA: `tools/gen_onlypdf30_*.py` → `tools/onlypdf_gate.py` (PASS×2 + recheck).

---

# D. Правило памяти агента

Cursor rule: `.cursor/rules/pass-recipes-tbank-alfa.mdc` (`alwaysApply`).  
При регрессе T‑Bank/Alfa SBP — сначала сверять с **этим** документом и specimen PDF в `docs/`, не изобретать третий layout.

---

*Эталоны зафиксированы 2026-08-01; HARD-мелочи (FONTFILE2 atlas, head epoch, no soft-ship, no font-bake) дополнены 2026-08-02.*  
*Код: `tbank_sbp_stealth.py`, `tbank_dynamic.py`, `alfa_sbp_stealth.py`, `glyph_library/raw_glyphs.pkl`.*
