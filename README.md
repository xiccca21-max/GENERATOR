# GENERATOR

Telegram-бот и Python-генераторы PDF-чеков. Для каждого банка и способа перевода есть **ровно одна** живая функция `create_*_stealth` — её вызывает `bot.py`. Параллельных «демо»-генераторов нет.

Живые банки в боте: **Сбер**, **Альфа**, **Т-Банк**.

## Быстрый старт

```text
python -m pip install -r requirements.txt
copy .env.example .env
```

В `.env` укажите `BOT_TOKEN` от [@BotFather](https://t.me/BotFather). Затем:

```text
python bot.py
```

В Telegram: выбрать банк → способ перевода → вставить поля по подсказке бота. Пустые / `авто` / `сейчас` бот заполняет сам.

Деплой на VPS (`/opt/receipt-bot`, systemd `receipt-bot`):

```text
python deploy/deploy.py
python deploy/deploy.py bot.py alfa_sbp_stealth.py
python deploy/deploy.py --full
```

Секреты (`deploy/.deploy.env`, `.env`, сессии Telegram) в git не кладутся.

## Генераторы по банкам

Канонический путь: **бот → `create_*_stealth`**. Проверка пачкой: `tools/gen_onlypdf30_*.py`.

### Т-Банк

| Способ в боте | Функция | Файл | QA-скрипт |
|---|---|---|---|
| СБП | `create_tbank_sbp_stealth` | `tbank_sbp_stealth.py` | `tools/gen_onlypdf30.py` |
| По телефону | `create_tbank_phone_stealth` | `tbank_phone_stealth.py` | `tools/gen_onlypdf30_phone.py` |
| Карта → Сбер | `create_tbank_stealth` | `tbank_stealth_v3.py` | `tools/gen_onlypdf30_card_sber.py` |
| Карта → Т-Банк | `create_tbank_card_tbank_stealth` | `tbank_card_tbank_stealth.py` | `tools/gen_onlypdf30_card_tbank.py` |
| Без комиссии | `create_tbank_nocomm_stealth` | `tbank_nocomm_stealth.py` | `tools/gen_onlypdf30_nocomm.py` |

Поля СБП: сумма, отправитель, телефон, получатель, банк получателя, дата, ID СБП (`авто`), номер квитанции (`авто`).

Поля телефона: сумма, отправитель, телефон, получатель, дата, номер квитанции.

Поля карты: сумма, отправитель, получатель, карта, банк, дата, комиссия, номер квитанции.

Дата на лице чека до 10.07.2026 → третий токен `/Keywords` = `991`; с 10.07.2026 → `DOCS-2035`. Колонка значений справа, край `x1 = 250`.

### Альфа-Банк

| Способ в боте | Функция | Файл | QA-скрипт |
|---|---|---|---|
| СБП | `create_alfa_sbp_stealth` | `alfa_sbp_stealth.py` | `tools/gen_onlypdf30_alfa_sbp.py` |
| Карта на карту | `create_alfa_card_stealth` | `alfa_card_stealth.py` | `tools/gen_onlypdf30_alfa_card.py` |
| По телефону | `create_alfa_phone_stealth` | `alfa_phone_stealth.py` | `tools/gen_onlypdf30_alfa_phone.py` |

Поля СБП: сумма, получатель, телефон, банк получателя, дата, счёт списания (20 цифр), номер операции, номер СБП, сообщение.

Поля карты: сумма, карта отправителя, карта получателя, дата, номер операции.

Поля телефона: сумма, получатель, телефон, дата, счёт, номер операции, сообщение.

Левая колонка `x0 = 35.45`, правая `x0 = 304.75`. Оболочка Oracle BI Publisher (СБП/карта) или Quartz/iOS (телефон).

### Сбербанк

| Способ в боте | Функция | Файл | QA-скрипт |
|---|---|---|---|
| СБП | `create_sber_sbp_stealth` | `sber_sbp_stealth.py` | `tools/gen_onlypdf30_sber_sbp.py` |
| По телефону | `create_sber_phone_stealth` | `sber_phone_stealth.py` | `tools/gen_onlypdf30_sber_phone.py` |
| Карта в другой банк | — | — | **закрыт** (нет чистого донора) |

Поля СБП и телефона: сумма, отправитель, получатель, телефон, банк получателя, дата. Значения слева, `x0 ≈ 21`.

## Вызов из Python

```python
from pathlib import Path
from tbank_sbp_stealth import create_tbank_sbp_stealth

pdf = create_tbank_sbp_stealth({
    "amount": "5000",
    "sender": "Павел Соколов",
    "receiver": "Роман С.",
    "phone": "+7 (900) 123-45-67",
    "recipient_bank": "Сбербанк",
    "date_time": "21.04.2026 20:54:15",
    "receipt_num": "авто",
    "sbp_id": "авто",
})
Path("tbank_sbp.pdf").write_bytes(pdf)
```

Альфа СБП:

```python
pdf = create_alfa_sbp_stealth({
    "amount": "5000",
    "receiver": "Анна Иванова",
    "phone": "+7 (900) 123-45-67",
    "recipient_bank": "Сбербанк",
    "date_time": "15.05.2026 12:34:15",
    "account": "авто",
    "operation_num": "авто",
    "sbp_id": "авто",
})
```

Сбер СБП:

```python
pdf = create_sber_sbp_stealth({
    "amount": "5500",
    "sender": "Анна Иванова",
    "receiver": "Павел Волков",
    "phone": "+7 (900) 123-45-67",
    "recipient_bank": "Т-Банк",
    "date_time": "12.06.2026 12:34:15",
})
```

Функция возвращает `bytes` PDF или `None`, если сборка не удалась.

## Пакетная проверка

```text
python tools/gen_onlypdf30_sber_sbp.py -n 5 --out output/sber_sbp_onlypdf
python tools/gen_onlypdf30_alfa_sbp.py -n 5 --out output/alfa_sbp_onlypdf
python tools/gen_onlypdf30.py -n 5 --out output/tbank_sbp_onlypdf
```

Полный прогон каналов: `python tools/run_all_onlypdf30.py`.

## Правила текста

- Кириллица и цифры без ограничений по длине.
- Буквы **ё** и **ъ** в обязательный алфавит не входят.
- Не подменять ФИО пользователя текстом с донора.
- Не добавлять второй генератор на тот же канал — только `create_*_stealth` выше.

## Закрытые каналы

Пока нет чистого донора и 30/30: ВТБ, ОТП, Ozon, Сбер «карта», выписка Т-Банка. Код может лежать в репозитории, в боте кнопок нет.

## Структура

| Путь | Зачем |
|---|---|
| `bot.py` | Telegram-бот |
| `*_stealth.py` | Живые генераторы |
| `templates/` | Донорские PDF |
| `tools/` | QA, OnlyPDF, ночной цикл |
| `deploy/deploy.py` | Выкладка на VPS |
| `docs/` | Рецепты PASS (Т-Банк СБП, Альфа СБП) |
