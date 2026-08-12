"""Сбербанк — перевод по карте в другой банк."""
from __future__ import annotations

import logging
from time_msk import now_msk
import os
import random
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
CARD_SHELL = os.path.join(_DIR, "templates", "sber_shells", "сбер по карте в другой банк.pdf")

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _format_card_date(date_raw: str, time_raw: str = "") -> str:
    date_raw = (date_raw or "").strip()
    time_raw = (time_raw or "").strip()
    if not re.search(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}", date_raw) and date_raw.lower() not in (
        "сейчас", "now", "-", "",
    ):
        now = now_msk()
        date_raw = now.strftime("%d.%m.%Y")
        time_raw = time_raw or now.strftime("%H:%M:%S")
    if not time_raw:
        time_raw = now_msk().strftime("%H:%M:%S")
    elif len(time_raw) == 5:
        time_raw = f"{time_raw}:00"
    if "." in date_raw:
        parts = date_raw.split(".")
        if len(parts) == 3:
            day, month, year = parts
            try:
                # Donor: "22 июня 2026 00:30:45  МСК" (две пробела перед МСК)
                return f"{int(day):02d} {_MONTHS[int(month) - 1]} {year} {time_raw}  МСК"
            except (ValueError, IndexError):
                pass
    now = now_msk()
    return (
        f"{now.day:02d} {_MONTHS[now.month - 1]} {now.year} "
        f"{time_raw or now.strftime('%H:%M:%S')}  МСК"
    )


def _format_amount_int(amount_raw: str) -> str:
    n = int(re.sub(r"\D", "", str(amount_raw) or "0") or "0")
    return f"{n:,}".replace(",", " ") + " ₽"


def _format_money_dec(rubles: int, kopecks: int = 0) -> str:
    head = f"{rubles:,}".replace(",", " ")
    return f"{head},{kopecks:02d} ₽"


def _format_card_mask(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 4:
        return f"•• {digits[-4:]}"
    if digits:
        return f"•• {digits.zfill(4)}"
    return f"•• {random.randint(1000, 9999)}"


def _prepare_card(data: Dict) -> Dict[str, str]:
    date_in = (data.get("date") or data.get("date_time") or "").strip()
    time_in = (data.get("time") or "").strip()
    if date_in.lower() in ("сейчас", "now", "-", ""):
        now = now_msk()
        date_in = now.strftime("%d.%m.%Y")
        time_in = time_in or now.strftime("%H:%M:%S")
    elif "," in date_in:
        date_in, time_in = [x.strip() for x in date_in.split(",", 1)]
    elif " " in date_in and "." in date_in.split()[0]:
        parts = date_in.split()
        date_in = parts[0]
        time_in = parts[1] if len(parts) > 1 else time_in

    amt = int(re.sub(r"\D", "", str(data.get("amount", "0"))) or "0")
    comm_raw = str(data.get("commission", "0")).strip()
    if "," in comm_raw or "." in comm_raw:
        cleaned = comm_raw.replace(" ", "").replace(",", ".")
        try:
            comm_f = float(re.sub(r"[^\d.]", "", cleaned) or "0")
            comm_rub = int(comm_f)
            comm_kop = int(round((comm_f - comm_rub) * 100))
        except ValueError:
            comm_rub, comm_kop = 0, 0
    else:
        comm_rub = int(re.sub(r"\D", "", comm_raw) or "0")
        comm_kop = 0

    charged_rub = amt + comm_rub
    charged_kop = comm_kop

    sender_card = (
        data.get("sender_card")
        or data.get("from_card")
        or f"•• {random.randint(1000, 9999)}"
    )
    dest_card = (
        data.get("dest_card")
        or data.get("card")
        or data.get("receiver_card")
        or data.get("receiver_phone")
        or ""
    )
    bank = str(
        data.get("bank")
        or data.get("bank_name")
        or data.get("recipient_bank")
        or "T-Bank"
    ).strip()
    # Donor keeps Latin city-like bank line; Cyrillic banks need font-patch.
    country = str(data.get("country") or "Россия").strip()

    return {
        "date_time": _format_card_date(date_in, time_in),
        "sender_name": str(
            data.get("sender_name") or data.get("sender") or data.get("receiver") or ""
        ).strip(),
        "sender_card": _format_card_mask(str(sender_card)),
        "dest_card": _format_card_mask(str(dest_card)),
        "amount": _format_amount_int(str(amt)),
        "commission": _format_money_dec(comm_rub, comm_kop),
        "charged": _format_money_dec(charged_rub, charged_kop),
        "bank": bank,
        "country": country,
    }


def create_sber_card_stealth(data: Dict) -> Optional[bytes]:
    """PDF «По карте в другой банк»."""
    from sber_dynamic import build_dynamic_sber_card

    if not os.path.isfile(CARD_SHELL):
        logger.error("Sber card shell not found: %s", CARD_SHELL)
        return None

    prepared = _prepare_card(data)
    result = build_dynamic_sber_card(prepared)
    if result:
        logger.info("Sber card OK (%d bytes)", len(result))
    return result
