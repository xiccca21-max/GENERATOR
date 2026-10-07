"""Сбербанк — перевод по карте в другой банк."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
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
        or ("•• " + "".join(random.choice("012345678") for _ in range(4)))
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
        or "Sankt-Peterburg"
    ).strip()
    # Donor face uses Latin city-like «Sankt-Peterburg»; Cyrillic banks need font-patch.
    # Never default to «T-Bank» — T/B absent from the card cmap without patch.
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


def _card_face_money(raw: str, *, kopecks: bool) -> str:
    text = (raw or "").strip()
    if "₽" in text or "\u20bd" in text:
        return text
    if kopecks:
        from sber_phone_stealth import _format_phone_money
        return _format_phone_money(text)
    n = int(re.sub(r"\D", "", text) or "0")
    return f"{n:,}".replace(",", " ") + " ₽"


def _card_mask(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "•• %04d" % random.randint(0, 9999)
    if re.fullmatch(r"[•*\s\d]+", text):
        digits = re.sub(r"\D", "", text)
        if digits:
            return "•• %s" % digits[-4:].zfill(4)
    return text


def _create_card_faithful(data: Dict) -> Optional[bytes]:
    """Сборка как у СБП: Jasper/iText, без правки чужого PDF и без iOS-пересохранения."""
    import sber_sbp_faithful as F
    from sber_phone_stealth import _phone_fio

    date_in = (data.get("date") or data.get("date_time") or "").strip()
    time_in = (data.get("time") or "").strip()
    label = _format_card_date(date_in, time_in)
    m = re.match(r"(\d+) (\S+) (\d+) (\d+):(\d+):(\d+)", label)
    if not m:
        return None
    months = {name: i + 1 for i, name in enumerate(_MONTHS)}
    when = datetime(
        int(m.group(3)), months[m.group(2)], int(m.group(1)),
        int(m.group(4)), int(m.group(5)), int(m.group(6)),
    )
    created = when + timedelta(seconds=random.randint(61, 199))
    now = now_msk().replace(microsecond=0)
    if created > now:
        created = max(when + timedelta(seconds=1), now)

    amount = _card_face_money(str(data.get("amount", "0")), kopecks=False)
    fee = _card_face_money(str(data.get("commission", "0")), kopecks=True)
    charged_raw = data.get("charged")
    if charged_raw:
        charged = _card_face_money(str(charged_raw), kopecks=True)
    else:
        from sber_phone_stealth import _parse_phone_money
        ar, ak = _parse_phone_money(amount)
        fr, fk = _parse_phone_money(fee)
        total_kop = ak + fk
        charged = _card_face_money(str(ar + fr + total_kop // 100) + "." + "%02d" % (total_kop % 100), kopecks=True)

    return F.build_card(
        when_msk=when,
        sender=_phone_fio(str(data.get("sender_name") or data.get("sender") or data.get("receiver") or "").strip()),
        account=_card_mask(str(data.get("sender_card") or data.get("from_card") or "")),
        dest=_card_mask(str(
            data.get("dest_card") or data.get("card") or data.get("receiver_card") or data.get("receiver_phone") or ""
        )),
        bank=str(data.get("bank") or data.get("bank_name") or data.get("recipient_bank") or "Сбербанк").strip(),
        country=str(data.get("country") or "Россия").strip(),
        amount=amount,
        fee=fee,
        charged=charged,
        created_msk=created,
    )


def create_sber_card_stealth(data: Dict) -> Optional[bytes]:
    """PDF «По карте в другой банк». Сначала сборка как у СБП."""
    if os.environ.get("SBER_CARD_LEGACY") != "1":
        try:
            res = _create_card_faithful(dict(data))
            if res:
                return res
        except Exception:
            logger.exception("Sber card faithful failed — legacy pipeline")
    return _create_sber_card_stealth_legacy(data)


def _create_sber_card_stealth_legacy(data: Dict) -> Optional[bytes]:
    """Прежний конвейер (правка донора и пересжатие под iOS)."""
    from sber_dynamic import build_dynamic_sber_card
    from openpdf_deflate import _pad_after_et_burned
    import fitz

    if not os.path.isfile(CARD_SHELL):
        logger.error("Sber card shell not found: %s", CARD_SHELL)
        return None

    prepared = _prepare_card(data)
    result = build_dynamic_sber_card(prepared)
    if not result:
        return None
    try:
        doc = fitz.open(stream=result, filetype="pdf")
        stream = doc.xref_stream(doc[0].get_contents()[0])
        try:
            text = doc[0].get_text() or ""
        except Exception:
            text = ""
        doc.close()
    except Exception:
        logger.warning("Sber card: reject unreadable candidate")
        return None
    if _pad_after_et_burned(stream):
        logger.warning("Sber card: reject pad-after-ET")
        return None
    bt, et = stream.count(b"BT"), stream.count(b"ET")
    if bt != et:
        logger.warning("Sber card: BT/ET mismatch %d/%d", bt, et)
        return None
    flat = re.sub(r"\s+", " ", (text or "").replace("\u202f", " ")).strip()
    # After font-patch MuPDF often OCR-garbles Identity-H amounts while the
    # CID stream is fine (Proton CLEAN). Enforce OCR only when all money
    # fields decode; otherwise skip.
    money_hits = 0
    for key in ("amount", "commission", "charged"):
        want = re.sub(r"\s+", " ", str(prepared.get(key) or "")).strip()
        if want and want in flat:
            money_hits += 1
    if money_hits < 3:
        logger.warning(
            "Sber card: fitz OCR money %d/3 — skip OCR gate (CID face)", money_hits,
        )
    try:
        from tools.emit_quality_gate import count_odd_tj
    except Exception:
        try:
            from emit_quality_gate import count_odd_tj
        except Exception:
            count_odd_tj = None  # type: ignore
    if count_odd_tj is not None:
        odd, total = count_odd_tj(result)
        if odd:
            logger.warning("Sber card: odd Tj %d/%d — reject", odd, total)
            return None
    result = quartz_flate_level1(result)
    logger.info("Sber card OK (%d bytes)", len(result))
    return result


def quartz_flate_level1(pdf: bytes) -> bytes:
    """iOS Quartz writes every Flate stream as zlib level 1 (header ``7801``).

    The Jasper-style builder re-deflates content/FontFile2 at the default level
    (``789c``). 58/58 Quartz originals are all-7801, so a mixed file is a
    structural tell. Re-deflate every non-7801 Flate stream at level 1 and
    rebuild /Length + classic xref. Returns the input unchanged on any doubt.
    """
    import zlib

    try:
        sx = re.search(rb"startxref\s+(\d+)\s*%%EOF\s*$", pdf)
        if not sx:
            return pdf
        xoff = int(sx.group(1))
        if pdf[xoff:xoff + 4] != b"xref":
            return pdf
        hm = re.match(rb"xref\s+(\d+)\s+(\d+)\s+", pdf[xoff:xoff + 64])
        if not hm:
            return pdf
        first, count = int(hm.group(1)), int(hm.group(2))
        tbl = xoff + hm.end()
        offsets: Dict[int, int] = {}
        for k in range(count):
            row = pdf[tbl + 20 * k: tbl + 20 * (k + 1)]
            if len(row) < 20 or row[17:18] != b"n":
                continue
            offsets[first + k] = int(row[:10])
        if not offsets:
            return pdf
        order = sorted(offsets.items(), key=lambda kv: kv[1])
        trailer_pos = pdf.find(b"trailer", tbl)
        if trailer_pos < 0:
            return pdf
        trailer = pdf[trailer_pos:sx.start()]

        changed = False
        bodies: Dict[int, bytes] = {}
        for idx, (num, start) in enumerate(order):
            end = order[idx + 1][1] if idx + 1 < len(order) else xoff
            if idx + 1 == len(order):
                end = xoff
            obj = pdf[start:end]
            lm = re.search(rb"/Length\s+(\d+)(?!\d)(?!\s+\d+\s+R)", obj)
            sm = re.search(rb"stream\r?\n", obj)
            if lm and sm and b"/FlateDecode" in obj[:sm.start()]:
                n = int(lm.group(1))
                data = obj[sm.end(): sm.end() + n]
                tail = obj[sm.end() + n:]
                if (
                    len(data) == n
                    and data[:1] == b"x"
                    and data[:2] != b"\x78\x01"
                    and tail.lstrip(b"\r\n").startswith(b"endstream")
                ):
                    dec = zlib.decompress(data)
                    comp = zlib.compress(dec, 1)
                    head = obj[:lm.start(1)] + str(len(comp)).encode() + obj[lm.end(1):sm.end()]
                    obj = head + comp + tail
                    changed = True
            bodies[num] = obj
        if not changed:
            return pdf

        out = bytearray(pdf[:order[0][1]])
        new_off: Dict[int, int] = {}
        for num, _s in order:
            new_off[num] = len(out)
            out.extend(bodies[num])
        new_xoff = len(out)
        rows = [b"xref\n", f"{first} {count}\n".encode()]
        for k in range(first, first + count):
            if k in new_off:
                rows.append(f"{new_off[k]:010d} 00000 n \n".encode())
            else:
                rows.append(b"0000000000 65535 f \n")
        out.extend(b"".join(rows))
        out.extend(trailer)
        out.extend(f"startxref\n{new_xoff}\n%%EOF\n".encode())
        res = bytes(out)
        import fitz
        fitz.open(stream=res, filetype="pdf").close()
        return res
    except Exception as exc:  # pragma: no cover — never ship a broken file
        logger.warning("quartz_flate_level1 skipped: %s", exc)
        return pdf
