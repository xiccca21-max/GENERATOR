# -*- coding: utf-8 -*-
"""Full-charset name pools for canonical OnlyPDF batches.

Includes short + long unusual FIO so batches stress real user inputs,
not only common bank-style names.
"""
from __future__ import annotations

# Alphabet goal includes every Russian letter, especially ё/ъ.
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
    ("Фёдор", "Подъячев"),  # ё ъ
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
    ("Алёна", "Объедкова"),  # ё ъ
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
    "Фёдор Ъ.",
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
    "Фёдор Подъячевич Ё.",  # ё ъ
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
    "Ёл Ё.",
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

# QA pool: ё/ъ/й not required (and not used). Never transliterate if they
# still reach a generator — hydrate instead.
_EXCLUDED = frozenset("ъЪёЁйЙ")


def _supported(value) -> bool:
    text = " ".join(value) if isinstance(value, tuple) else str(value)
    return not any(ch in text for ch in _EXCLUDED)


MALE = [value for value in MALE if _supported(value)]
FEMALE = [value for value in FEMALE if _supported(value)]
UNUSUAL_SENDERS = [value for value in UNUSUAL_SENDERS if _supported(value)]
STRESS_LONG_SENDERS = [value for value in STRESS_LONG_SENDERS if _supported(value)]
RECV_SHORT = [value for value in RECV_SHORT if _supported(value)]
STRESS_LONG_RECV = [value for value in STRESS_LONG_RECV if _supported(value)]
SBER_FIO_LONG = [value for value in SBER_FIO_LONG if _supported(value)]
SBER_FIO_SHORT = [value for value in SBER_FIO_SHORT if _supported(value)]
BANKS_MULTI = [value for value in BANKS_MULTI if _supported(value)]
RECV_FULL = [value for value in RECV_FULL if _supported(value)]

CYR_FULL = "абвгдежзиклмнопрстуфхцчшщыьэюя"
CYR_NO_YO_TVERD = CYR_FULL

# Complex FIO for T-Bank 10/10 — rare letters except ё/ъ/й. Attempt does not
# swap to short easy names.
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
]
HARD_TBANK_SENDERS = [v for v in HARD_TBANK_SENDERS if _supported(v)]
HARD_TBANK_RECV = [v for v in HARD_TBANK_RECV if _supported(v)]


def pick_hard_tbank_pair(i: int, attempt: int = 0) -> tuple[str, str]:
    return HARD_TBANK_SENDERS[(i - 1 + attempt) % len(HARD_TBANK_SENDERS)]


def pick_hard_tbank_recv(i: int, attempt: int = 0) -> str:
    return HARD_TBANK_RECV[(i - 1 + attempt * 3) % len(HARD_TBANK_RECV)]


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
            return table[i]
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
            return opts[attempt % len(opts)]
        return table[i]
    off = 0 if role == "recv" else 7
    if attempt >= 3:
        return SBER_FIO_SHORT[(i * 5 + attempt + off) % len(SBER_FIO_SHORT)]
    if (i + attempt + off) % 2 == 0 and i >= 10:
        return SBER_FIO_LONG[(i * 3 + attempt + off) % len(SBER_FIO_LONG)]
    return SBER_FIO_SHORT[(i * 5 + attempt + off) % len(SBER_FIO_SHORT)]


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
