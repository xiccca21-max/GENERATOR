# -*- coding: utf-8 -*-
"""Full-charset name pools for canonical OnlyPDF batches.

Includes short + long unusual FIO so batches stress real user inputs,
not only common bank-style names.
"""
from __future__ import annotations

import random
MALE = [
    ("Игорь", "Соколов"),
    ("Павел", "Волков"),
    ("Артем", "Козлов"),
    ("Дмитрий", "Новиков"),
    ("Сергей", "Семенов"),
    ("Алексей", "Павлов"),
    ("Максим", "Орлов"),
    ("Никита", "Макаров"),
    ("Андрей", "Борисов"),
    ("Кирилл", "Громов"),
    ("Илья", "Тихонов"),
    ("Владимир", "Медведев"),
    ("Юрий", "Цветков"),
    ("Ярослав", "Кузнецов"),
    ("Федор", "Рыбаков"),  # ы
    ("Роман", "Щеглов"),  # щ (not Щукин — burned family)
    ("Харитон", "Хромов"),  # х
    ("Эдуард", "Харитонов"),  # э х
    ("Глеб", "Шаров"),  # ш
    ("Федор", "Подъячев"),  # ъ
]
FEMALE = [
    ("Елена", "Козлова"),
    ("Мария", "Смирнова"),
    ("Ольга", "Морозова"),
    ("Анна", "Лебедева"),
    ("Наталья", "Егорова"),
    ("Юлия", "Ковалева"),
    ("Татьяна", "Зайцева"),
    ("Виктория", "Никитина"),
    ("Екатерина", "Савельева"),
    ("Полина", "Белова"),
    ("Алина", "Крылова"),  # ы
    ("Жанна", "Жукова"),  # ж
    ("Элина", "Михайлова"),  # э
    ("Дарья", "Соколова"),
    ("Алена", "Сысоева"),  # ы
    ("Цветана", "Яшина"),  # ц ш
    ("Оксана", "Чернова"),  # ч
    ("Инна", "Щербакова"),  # щ
    ("Алена", "Объедкова"),  # ъ
]

# Unusual but *realistic* senders — OnlyPDF FAKE на шуточных ФИО
# (Ырка/Эльф/Спрашивай). Редкие буквы + длинные/нестандартные имена.
UNUSUAL_SENDERS = [
    ("Элина", "Юрьева"),      # э ю
    ("Юрий", "Щербаков"),     # ю щ
    ("Яков", "Цветков"),      # я ц
    ("Жанна", "Жукова"),      # ж
    ("Харитон", "Хромов"),    # х
    ("Федор", "Шаров"),       # ф ш
    ("Инна", "Щербакова"),    # щ
    ("Оксана", "Чернова"),    # ч
    ("Алена", "Сысоева"),     # ы
    ("Роман", "Щеглов"),      # щ
    ("Павел", "Рыбаков"),     # ы
    ("Цветана", "Яшина"),     # ц я
    ("Эдуард", "Харитонов"),  # э х
    ("Татьяна", "Йонова"),    # й (без ё)
    ("Фёдор", "Подъячев"),     # ё ъ
    ("Мухаммад", "Алиев"),
    ("Али", "Магомедов"),
    ("Абдулрахман", "Гаджиев"),
    ("Рамазан", "Шихсаидов"),
    ("Хаджимурат", "Абдуллаев"),
    ("Зульфия", "Каримова"),
    ("Гульнара", "Низамова"),
    ("Айгуль", "Фахрутдинова"),
    ("Бахтияр", "Юсупов"),
    ("Шамиль", "Ибрагимов"),
    ("Джамбулат", "Цахаев"),
    ("Магомед-Али", "Омаров"),
    ("Нурсултан", "Бекмурзаев"),
    ("Александр-Магомед", "Петров"),
    ("Константин", "Христорождественский"),
    ("Владислав", "Преображенский"),
    ("Екатерина", "Вознесенская"),
    ("Михаил", "Волков"),
    ("Дарья", "Белова"),
    ("Кирилл", "Громов"),
    ("Наталья", "Козлова"),
    ("Игорь", "Соколов"),
    ("Мария", "Новикова"),
    ("Антон", "Морозов"),
    ("Елена", "Павлова"),
    ("Сергей", "Лебедев"),
    ("Ольга", "Кузнецова"),
    ("Андрей", "Попов"),
]

# Extra long / rare-letter FIO for night Proton stress (any letters/digits face).
STRESS_LONG_SENDERS = [
    ("Мухаммад", "Абдулвахитович Алиев"),
    ("Али", "Магомедрасулович Гаджиев"),
    ("Абдурахман", "Хаджимуратович Омаров"),
    ("Рамазан", "Шихсаидович Цахаев"),
    ("Зульфия", "Фарид кызы Каримова"),
    ("Гульназ", "Ринатовна Фахрутдинова"),
    ("Бахтияр", "Нурсултанович Юсупов"),
    ("Шамиль", "Джамбулатович Ибрагимов"),
    ("Харитон", "Щеколдович Хрусталев"),
    ("Цветана", "Чудиновна Яшина"),
    ("Жанна", "Жемчужникова"),
    ("Эльдар", "Хафизович Шарафутдинов"),
    ("Константин", "Христорождественский"),
    ("Владислава", "Преображенская"),
    ("Айгуль", "Зульфировна Низамова"),
    ("Магомед", "Алиевич Абдуллаев"),
    ("Нурлан", "Бекмурзаев"),
    ("Саид-Ахмед", "Магомедов"),
    ("Роман", "Щеглов"),
    ("Инна", "Щербакова"),
    ("Федор", "Рыбаков"),
    ("Павел", "Сысоев"),
    ("Юрий", "Шаров"),
    ("Глеб", "Шишкин"),
    ("Оксана", "Шершнева"),
    ("Эльвира", "Шамсутдинова"),
]

STRESS_LONG_RECV = [
    "Мухаммад А.",
    "Али М.",
    "Абдулрахман Г.",
    "Хаджимурат О.",
    "Зульфия К.",
    "Гульнара Н.",
    "Бахтияр Ю.",
    "Шамиль И.",
    "Джамбулат Ц.",
    "Нурсултан Б.",
    "Харитон Х.",
    "Цветана Я.",
    "Жанна Ж.",
    "Эльдар Ш.",
    "Щербак Щ.",
    "Рыбак Ы.",
]

# Short receivers (T-Bank style Имя X.)
RECV_SHORT = [
    "Анна А.",
    "Ольга О.",
    "Павел П.",
    "Мария М.",
    "Игорь И.",
    "Ян Ю.",
    "Эд Э.",
    "Федор Ф.",
    "Жанна Ж.",
    "Цвета Ц.",
    "Инна Щ.",
    "Хари Х.",
    "Алена Ы.",
    "Виталий Б.",
    "Федор Ъ.",
]

# Long / weird FIO for Sber (Имя Отчество Ф. or full) — mixed lengths.
# Must collectively cover CYR_NO_YO_TVERD (esp. ж й х ш ы ь).
SBER_FIO_LONG = [
    "Катык Предсказуемович Е.",
    "Мария Ссылковна З.",
    "Зюзяк Невероятович Щ.",
    "Фырчик Щеколдович Х.",
    "Элина Ювелировна Ц.",
    "Харитон Хрусталевич Ж.",  # х ж
    "Цветана Чудиновна Ф.",
    "Жмых Жарович Ы.",  # ж ы
    "Ырка Ыгнатович Ш.",  # ы ш
    "Шаман Шарипович Ю.",  # ш
    "Бзик Боярышникович Я.",
    "Грохот Гвоздикович В.",
    "Дыня Драконовна Г.",
    "Кряква Кружевникович Д.",
    "Мямлик Морковкинович К.",
    "Инна Щербаковна Л.",
    "Роман Щеглович М.",
    "Оксана Черновна Н.",
    "Игорь Тихонович П.",  # ь й
    "Вячеслав Борисович Р.",  # ь
    "Юрий Цветкович Т.",  # й ю
    "Павел Шарович У.",  # ш
    "Федор Подъячевич Е.",  # ъ
]
SBER_FIO_SHORT = [
    "Ян Ю.",
    "Эд Э.",
    "Ли Л.",
    "Фо Ф.",
    "Жо Ж.",  # ж
    "Щу Щ.",
    "Хи Х.",  # х
    "Цо Ц.",
    "Ыр Ы.",  # ы
    "Ша Ш.",  # ш
    "Игорь И.",  # ь
    "Юрий Й.",  # й ю
    "Ел Е.",
    "Ъян Ъ.",
]

BANKS_MULTI = [
    "Сбербанк",
    "Альфа-Банк",
    "ВТБ",
    "Газпромбанк",
    "ПСБ",
    "Райффайзен Банк",
    "Озон Банк",
]
RECV_FULL = [
    "Анна Алексеева",
    "Ольга Петрова",
    "Павел Николаев",
    "Мария Васильева",
    "Игорь Федоров",
    "Катык Предсказуем",
    "Спрашивай Чувства",
    "Фырчик Щеколдин",
]

# Full charset (reference). Live batches strip ё from faces — user law.
CYR_FULL = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
CYR_NO_YO = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
CYR_NO_YO_TVERD = CYR_NO_YO

_MASH_ROWS = (
    "цукенгшщзх",
    "фывапролджэ",
    "ячсмитьбю",
    "ыэщжцшхчю",
)
_NO_START = frozenset("ьыЬЫ")
# ъ has no shell-scale raw-glyph cache entry — mash/random faces with it ship donor FIO.
_BATCH_FACE_BLOCKED = frozenset("ъЪ")
_MASH_BLOCKED = frozenset("ъЪ")

_USED_FACE_KEYS: set[str] = set()
_USED_SENDER_KEYS: set[str] = set()


def batch_face_chars_ok(*names: str) -> bool:
    """Batch/marathon: skip FIO that cannot hydrate onto T-Bank SBP shells."""
    blob = "".join(str(n or "") for n in names)
    return not any(c in _BATCH_FACE_BLOCKED for c in blob)


def strip_yo(text: str) -> str:
    """Never ship ё on batch faces — map to е."""
    return (
        str(text or "")
        .replace("Ё", "Е")
        .replace("ё", "е")
    )


def _rng_slot(i: int, attempt: int, *, salt: int = 0) -> random.Random:
    import time
    tick = int(time.time()) // 17
    return random.Random(88072026 + i * 1597 + attempt * 311 + salt + tick)


def _mash_word(rng: random.Random, n: int) -> str:
    row = list(rng.choice(_MASH_ROWS))
    rare = [
        c for c in CYR_NO_YO
        if c not in "".join(_MASH_ROWS) and c not in _MASH_BLOCKED
    ]
    pool = row + rare
    n = max(4, min(n, 18))
    chars: list[str] = []
    for k in range(n):
        pick = rng.choice(pool)
        if k == 0 and pick in _NO_START:
            pick = rng.choice([c for c in pool if c not in _NO_START] or pool)
        chars.append(pick)
    return chars[0].upper() + "".join(chars[1:]).lower()


def _mash_tbank_sender(rng: random.Random) -> str:
    a = _mash_word(rng, rng.randint(6, 11))
    b = _mash_word(rng, rng.randint(9, 16))
    ini = rng.choice("АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ")
    if rng.random() < 0.45:
        return f"{a} {b} {ini}."
    return f"{a} {b}"


def _mash_tbank_recv(rng: random.Random) -> str:
    a = _mash_word(rng, rng.randint(5, 9))
    ini = rng.choice("АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ")
    return f"{a} {ini}."


def _short_tbank_sender(rng: random.Random, i: int) -> str:
    first = rng.choice(
        ("Ян", "Ли", "Фо", "Жо", "Хи", "Цо", "Ша", "Юрий", "Эд", "Глеб", "Павел")
    )
    ini = rng.choice("АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ")
    return f"{first} {ini}."


def _long_tbank_sender(i: int, attempt: int) -> str:
    first, last = STRESS_LONG_SENDERS[(i + attempt) % len(STRESS_LONG_SENDERS)]
    return strip_yo(f"{first} {last}")


def remember_face(sender: str, receiver: str) -> None:
    key = f"{strip_yo(sender).casefold()}|{strip_yo(receiver).casefold()}"
    _USED_FACE_KEYS.add(key)
    _USED_SENDER_KEYS.add(strip_yo(sender).casefold())


def pick_diverse_tbank_face(i: int, attempt: int = 0) -> tuple[str, str]:
    """Short / medium / long / heavy mash — unique per slot, no ё."""
    rng = _rng_slot(i, attempt)
    mode = (i * 3 + attempt) % 5
    for extra in range(32):
        if mode == 0:
            sender = strip_yo(_short_tbank_sender(rng, i + attempt + extra))
            receiver = strip_yo(RECV_SHORT[(i + attempt + extra) % len(RECV_SHORT)])
        elif mode == 1:
            fn, ln = HARD_TBANK_SENDERS[(i + attempt + extra) % len(HARD_TBANK_SENDERS)]
            sender = strip_yo(f"{fn} {ln}")
            receiver = strip_yo(HARD_TBANK_RECV[(i + attempt + extra * 3) % len(HARD_TBANK_RECV)])
        elif mode == 2:
            sender = _long_tbank_sender(i + attempt + extra, attempt)
            receiver = strip_yo(STRESS_LONG_RECV[(i + attempt + extra) % len(STRESS_LONG_RECV)])
        elif mode == 3:
            sender = strip_yo(_mash_tbank_sender(_rng_slot(i, attempt, salt=extra)))
            receiver = strip_yo(HARD_TBANK_RECV[(i + attempt + extra) % len(HARD_TBANK_RECV)])
        else:
            fn, ln = HARD_TBANK_SENDERS[(i + attempt + extra) % len(HARD_TBANK_SENDERS)]
            sender = strip_yo(f"{fn} {ln}")
            receiver = strip_yo(_mash_tbank_recv(_rng_slot(i, attempt, salt=extra + 7)))
        key = f"{sender.casefold()}|{receiver.casefold()}"
        if (
            key not in _USED_FACE_KEYS
            and sender.casefold() not in _USED_SENDER_KEYS
            and batch_face_chars_ok(sender, receiver)
        ):
            remember_face(sender, receiver)
            return sender, receiver
        rng = _rng_slot(i, attempt, salt=extra + 1)
    sender, receiver = pick_diverse_tbank_face(i + 997, attempt + 1)
    return sender, receiver


def pick_diverse_sber_face(i: int, attempt: int = 0, *, role: str = "recv") -> str:
    """Sber: short / long / mash / rare-letter — no ё."""
    rng = _rng_slot(i, attempt, salt=17 if role == "recv" else 29)
    mode = (i + attempt + (0 if role == "recv" else 2)) % 4
    if mode == 0:
        return strip_yo(SBER_FIO_SHORT[(i + attempt) % len(SBER_FIO_SHORT)])
    if mode == 1:
        return strip_yo(SBER_FIO_LONG[(i + attempt) % len(SBER_FIO_LONG)])
    if mode == 2:
        if role == "recv":
            return strip_yo(
                f"{_mash_word(rng, 6)} {_mash_word(rng, 9)} "
                f"{rng.choice('АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ')}"
            )
        return strip_yo(
            f"{_mash_word(rng, 5)} {_mash_word(rng, 8)} "
            f"{rng.choice('АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ')}."
        )
    return strip_yo(pick_sber_fio(i, attempt, role=role))


def diverse_amount(i: int, attempt: int = 0) -> str:
    rng = _rng_slot(i, attempt, salt=41)
    pool = (
        1200, 2500, 4500, 7800, 9470, 12000, 15500, 19999,
        25000, 33333, 38820, 42000, 55555, 67000, 88888, 99999,
        10500, 44800, 81234, 156789, 22024, 93934, 125500,
    )
    base = pool[(i + attempt * 3) % len(pool)]
    jitter = rng.randint(-97, 97) * ((-1) ** (i + attempt))
    val = max(1000, min(abs(base + jitter), 999999))
    return str(val)


def diverse_mobile_phone(i: int, attempt: int = 0) -> str:
    rng = _rng_slot(i, attempt, salt=53)
    digs = list("0123456789")
    rng.shuffle(digs)
    body = digs[:10]
    body[0] = "9"
    body[1] = str((i + attempt) % 10)
    body[2] = str((i * 3 + attempt) % 10)
    s = "".join(body)
    return f"+7 ({s[:3]}) {s[3:6]}-{s[6:8]}-{s[8:10]}"

# Complex FIO for T-Bank — rare letters + long names (any length).
HARD_TBANK_SENDERS = [
    ("Харитон", "Хрусталев"),
    ("Элина", "Белова"),
    ("Цветана", "Яшина"),
    ("Жанна", "Жукова"),
    ("Роман", "Щербаков"),
    ("Федор", "Фахрутдинов"),
    ("Павел", "Рыбаков"),
    ("Глеб", "Волков"),
    ("Оксана", "Чернова"),
    ("Зульфия", "Каримова"),
    ("Дмитрий", "Ликс"),
    ("Борис", "Новиков"),
    ("Сергей", "Семенов"),
    ("Татьяна", "Соколова"),
    ("Юлия", "Ковалева"),
    ("Анна", "Лебедева"),
    ("Никита", "Орлов"),
    ("Владимир", "Шаров"),
    ("Максим", "Попов"),
    ("Алексей", "Уткин"),
    ("Федор", "Подъячев"),  # ъ
    ("Алена", "Объедкова"),  # ъ
    ("Татьяна", "Йонова"),  # й
    ("Мухаммад", "Абдулвахитович Алиев"),
    ("Константин", "Христорождественский"),
    ("Владислав", "Преображенский"),
    ("Абдурахман", "Хаджимуратович Омаров"),
    ("Эльвира", "Шамсутдинова"),
    ("Щербак", "Щеколдов"),
    ("Ырысбек", "Сысоев"),
]
HARD_TBANK_RECV = [
    "Харитон Х.",
    "Элина Э.",
    "Цветана Я.",
    "Жанна Ж.",
    "Роман Щ.",
    "Федор Ф.",
    "Павел Р.",
    "Глеб Г.",
    "Оксана Ч.",
    "Зульфия К.",
    "Слава Б.",
    "Борис Н.",
    "Сергей С.",
    "Татьяна Т.",
    "Юлия Ю.",
    "Анна А.",
    "Никита О.",
    "Владимир Ш.",
    "Максим М.",
    "Алексей У.",
    "Федор Ъ.",
    "Алена Е.",
    "Юрий Й.",
    "Мухаммад А.",
    "Христорождественский К.",
    "Эльвира Ш.",
    "Щербак Щ.",
    "Ырысбек Ы.",
]


def pick_hard_tbank_pair(i: int, attempt: int = 0) -> tuple[str, str]:
    fn, ln = HARD_TBANK_SENDERS[(i - 1 + attempt) % len(HARD_TBANK_SENDERS)]
    return strip_yo(fn), strip_yo(ln)


def pick_hard_tbank_recv(i: int, attempt: int = 0) -> str:
    return strip_yo(HARD_TBANK_RECV[(i - 1 + attempt * 3) % len(HARD_TBANK_RECV)])


def coverage_report(text: str) -> tuple[list[str], list[str]]:
    low = text.lower()
    miss = [ch for ch in CYR_NO_YO_TVERD if ch not in low]
    dig_miss = [d for d in "0123456789" if d not in text]
    return miss, dig_miss


def pick_sender_pair(i: int, attempt: int = 0) -> tuple[str, str]:
    """Alternate unusual / standard pools; prefer unusual on even indices."""
    if (i + attempt) % 3 != 2:
        return UNUSUAL_SENDERS[(i * 5 + attempt * 3) % len(UNUSUAL_SENDERS)]
    use_f = (i + attempt) % 2 == 0
    pool = FEMALE if use_f else MALE
    return pool[(i * 5 + attempt * 3) % len(pool)]


def pick_receiver_short(i: int, attempt: int = 0) -> str:
    return RECV_SHORT[(i + attempt) % len(RECV_SHORT)]


ALFA_SBP_FIO = [
    "Алина Александровна А",
    "Павел Иванович С",
    "Жанна Евгеньевна Ж",
    "Харитон Петрович Х",
    "Элина Сергеевна Э",
    "Федор Николаевич Ф",
    "Цветана Юрьевна Ц",
    "Инна Михайловна Щ",
    "Роман Владимирович Щ",
    "Оксана Андреевна Ч",
    "Юрий Алексеевич Ц",
    "Алена Дмитриевна Ы",
]


def pick_alfa_sbp_receiver(i: int, attempt: int = 0) -> str:
    """Alfa SBP face: full first + full patronymic + surname initial, no period."""
    return ALFA_SBP_FIO[(i + attempt) % len(ALFA_SBP_FIO)]


def pick_sber_fio(i: int, attempt: int = 0, *, role: str = "recv") -> str:
    """Mix long unusual and short FIO for Sber fields; force rare letters.

    Rare-letter slots use natural short/full forms (OnlyPDF-safe).
    Long unusual FIO on higher indices; later attempts prefer short.
    """
    force_recv = {
        1: "Жанна Жукова",
        2: "Харитон Хромов",
        3: "Павел Шаров",
        4: "Федор Рыбаков",  # ы
        5: "Игорь Соколов",  # ь
        6: "Юрий Цветков",  # й ю
        7: "Роман Щеглов",  # щ
        8: "Цветана Яшина",  # ц
    }
    force_snd = {
        1: "Оксана Чернова",  # ч
        2: "Элина Михайлова",  # э
        3: "Инна Щербакова",  # щ
        4: "Алена Сысоева",  # ы
        5: "Вячеслав Борисов",  # ь
        6: "Харитон Хромов",  # х
        7: "Жанна Жукова",  # ж
        8: "Павел Шаров",  # ш
    }
    table = force_recv if role == "recv" else force_snd
    # On retries rotate force variants so OnlyPDF flakes/burns don't stuck.
    if i in table:
        if attempt == 0:
            return strip_yo(table[i])
        # keep rare letter: fall through to pools that still contain it
        rare_pools = {
            "ж": ["Жанна Жукова", "Елена Козлова"],
            "х": ["Харитон Хромов", "Эдуард Харитонов"],
            "ш": ["Павел Шаров", "Глеб Шаров"],
            "ы": ["Федор Рыбаков", "Алена Сысоева", "Алина Крылова"],
            "ь": ["Игорь Соколов", "Вячеслав Борисов"],
            "й": ["Юрий Цветков", "Андрей Борисов"],
            "щ": ["Роман Щеглов", "Инна Щербакова"],
            "ц": ["Цветана Яшина", "Юрий Цветков"],
            "ч": ["Оксана Чернова"],
            "э": ["Элина Михайлова", "Эдуард Харитонов"],
        }
        # map slot → letter key
        letter_by_slot_recv = {1: "ж", 2: "х", 3: "ш", 4: "ы", 5: "ь", 6: "й", 7: "щ", 8: "ц"}
        letter_by_slot_snd = {1: "ч", 2: "э", 3: "щ", 4: "ы", 5: "ь", 6: "х", 7: "ж", 8: "ш"}
        letter = (letter_by_slot_recv if role == "recv" else letter_by_slot_snd).get(i)
        if letter and letter in rare_pools:
            opts = rare_pools[letter]
            return strip_yo(opts[attempt % len(opts)])
        return strip_yo(table[i])
    off = 0 if role == "recv" else 7
    if attempt >= 3:
        return strip_yo(SBER_FIO_SHORT[(i * 5 + attempt + off) % len(SBER_FIO_SHORT)])
    if (i + attempt + off) % 2 == 0 and i >= 10:
        return strip_yo(SBER_FIO_LONG[(i * 3 + attempt + off) % len(SBER_FIO_LONG)])
    return strip_yo(SBER_FIO_SHORT[(i * 5 + attempt + off) % len(SBER_FIO_SHORT)])


def force_rare_pair(i: int) -> tuple[str, str] | None:
    """Fixed slots guaranteeing rare letters via realistic FIO."""
    force = {
        2: ("Элина", "Михайлова"),
        3: ("Федор", "Рыбаков"),
        4: ("Харитон", "Хромов"),
        5: ("Жанна", "Жукова"),
        7: ("Эдуард", "Харитонов"),
        8: ("Алена", "Хохлова"),
        9: ("Роман", "Щеглов"),
        11: ("Оксана", "Чернова"),
        12: ("Цветана", "Яшина"),
        13: ("Павел", "Шаров"),
        15: ("Инна", "Щербакова"),
        17: ("Алена", "Сысоева"),
        19: ("Юрий", "Цветков"),
        21: ("Федор", "Щеколдин"),
        23: ("Элина", "Юрьева"),
        25: ("Яков", "Цветков"),
        27: ("Харитон", "Жуков"),
        29: ("Инна", "Боярышникова"),
    }
    return force.get(i)
