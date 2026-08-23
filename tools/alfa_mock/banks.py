"""Bank aliases — same list as T-Bank mock bot, plus Alfa history logos/subtitles."""

from __future__ import annotations

ALLOWED_LABELS = (
    "Сбербанк",
    "Озон",
    "Т-Банк",
    "Альфа-Банк",
    "ВТБ",
    "Райффайзен",
)

# Live dump: field is logoUrl. Ozon=logo_bank_ozon_ecom, T-Bank=logo_bank_tinkoff_v2.
_LOGO_CDN = "https://alfaonline.servicecdn.ru/public/s3/static/logo-payments/"


def _icon(slug: str) -> str:
    return f"{_LOGO_CDN}logo_bank_{slug}__xl.png"


def _bank(name, short, category_name, key, needles, slug, extra=None):
    url = _icon(slug)
    row = {
        "name": name,
        "short": short,
        "categoryName": category_name,
        "key": key,
        "needles": needles,
        "logoUrl": url,
        "iconUrl": url,
        "logo": url,
    }
    if extra:
        row.update(extra)
    return row


_BANKS: dict[str, dict] = {
    "сбер": _bank("Сбербанк", "Сбербанк", "Сбербанк", "Сбер", ["сбер", "sber"], "sberbank"),
    "альфа": _bank("Альфа-Банк", "Альфа-Банк", "Альфа-Банк", "Альфа", ["альфа", "alfa"], "alfabank"),
    "ozon": _bank(
        "Озон Банк (Ozon)",
        "Озон",
        "Озон (Еком Банк)",
        "Ozon",
        ["озон", "ozon", "еком"],
        "ozon_ecom",
        {"logoFile": "finance-ozon-2.png", "baseColor": "005bff", "baseTextColor": "ffffff"},
    ),
    "т-банк": _bank("Т-Банк", "Т-Банк", "Т-Банк", "Т-Банк", ["т-банк", "тинькоф", "t-bank", "tbank", "tinkoff"], "tinkoff_v2"),
    "райффайзен": _bank("Райффайзенбанк", "Райффайзен", "Райффайзен", "Райффайзен", ["райф", "raiffeisen"], "raiffeisen"),
    "втб": _bank("ВТБ", "ВТБ", "ВТБ", "ВТБ", ["втб", "vtb"], "vtb"),
}

_ALIASES: dict[str, str] = {
    "сбер": "сбер",
    "сбербанк": "сбер",
    "sber": "сбер",
    "sberbank": "сбер",
    "альфа": "альфа",
    "альфабанк": "альфа",
    "альфа-банк": "альфа",
    "alfa": "альфа",
    "alfabank": "альфа",
    "ozon": "ozon",
    "озон": "ozon",
    "озон банк": "ozon",
    "ozon банк": "ozon",
    "озон банк (ozon)": "ozon",
    "ozon банк (ozon)": "ozon",
    "ozon bank": "ozon",
    "т-банк": "т-банк",
    "тбанк": "т-банк",
    "t-bank": "т-банк",
    "tbank": "т-банк",
    "тинькофф": "т-банк",
    "tinkoff": "т-банк",
    "райффайзен": "райффайзен",
    "райф": "райффайзен",
    "raiffeisen": "райффайзен",
    "втб": "втб",
    "vtb": "втб",
}


def _norm(raw: str) -> str:
    s = (raw or "").strip().lower().replace("ё", "е")
    s = " ".join(s.split())
    return s


_TBANK_OZON = {
    "name": "Ozon Банк (Ozon)",
    "logoFile": "finance-ozon-2.png",
    "baseColor": "005bff",
    "baseTextColor": "ffffff",
    "logo": "https://brands-prod.cdn-tinkoff.ru/general_logo/finance-ozon-2.png",
    "fileLink": "https://brands-prod.cdn-tinkoff.ru/general_logo/finance-ozon-2.png",
}

_TBANK_NAMES = {
    "сбер": "Сбербанк",
    "альфа": "Альфа-Банк",
    "ozon": "Ozon Банк (Ozon)",
    "т-банк": "Т-Банк",
    "райффайзен": "Райффайзенбанк",
    "втб": "ВТБ",
}


def unknown_bank(raw: str, cabinet: str = "alfa") -> dict:
    label = (raw or "").strip() or "Банк"
    broken = _LOGO_CDN + "logo_bank_not_found__xl.png"
    if cabinet == "tbank":
        return {
            "name": label,
            "unknown": True,
            "logoUrl": broken,
            "logo": broken,
            "fileLink": broken,
        }
    return {
        "name": label,
        "short": label,
        "categoryName": label,
        "key": "unknown",
        "needles": [],
        "logoUrl": broken,
        "iconUrl": broken,
        "logo": broken,
        "unknown": True,
    }


def resolve_bank(raw: str, cabinet: str = "alfa") -> dict:
    key = _ALIASES.get(_norm(raw))
    if not key:
        return unknown_bank(raw, cabinet)
    if cabinet == "tbank":
        if key == "ozon":
            return dict(_TBANK_OZON)
        return {"name": _TBANK_NAMES[key]}
    return dict(_BANKS[key])
