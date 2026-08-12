"""
SBER STEALTH v3 - Чистая байтовая замена с пересчётом xref
Автоматически выбирает шаблон с нужными буквами.

Структура Sberbank PDF:
- Текст в BT...ET блоках
- Позиция задаётся через "1 0 0 1 X Y Tm"
- Текст через "(CID_bytes)Tj" - одиночная строка
- CID = 2 байта на символ
- Все поля выровнены по левому краю (X=21)
"""

import fitz
import zlib
import re
import logging
from typing import Dict, Optional, List, Tuple

# Импорт менеджера шаблонов
try:
    from template_manager_sber import find_template_for_data, get_missing_chars_for_data, get_templates_info
    TEMPLATE_MANAGER_AVAILABLE = True
except ImportError:
    TEMPLATE_MANAGER_AVAILABLE = False

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(levelname)s | %(message)s')

# CID маппинг из анализа sber_original.pdf
# ВАЖНО: Это символы которые РЕАЛЬНО есть в шрифте шаблона!
CID = {
    ' ': 0x0003,
    '(': 0x000B,
    ')': 0x000C,
    '+': 0x000E,
    '-': 0x0010,
    '.': 0x0011,
    '0': 0x0013,
    '1': 0x0014,
    '2': 0x0015,
    '3': 0x0016,
    '4': 0x0017,
    '5': 0x0018,
    '6': 0x0019,
    '7': 0x001A,
    '8': 0x001B,
    '9': 0x001C,
    ':': 0x001D,
    'B': 0x0025,
    '•': 0x0087,
    # Кириллица ЗАГЛАВНАЯ (только доступные!)
    'А': 0x023A,
    'Б': 0x023B,
    'Д': 0x023E,
    'И': 0x0242,
    'К': 0x0244,
    'М': 0x0246,
    'Н': 0x0247,
    'О': 0x0248,
    'П': 0x0249,
    'Р': 0x024A,
    'С': 0x024B,
    'Т': 0x024C,
    'Ф': 0x024E,
    'Ч': 0x0251,
    'Ю': 0x0258,
    # Кириллица строчная (только доступные!)
    'а': 0x025A,
    'б': 0x025B,
    'в': 0x025C,
    'д': 0x025E,
    'е': 0x025F,
    'и': 0x0262,
    'й': 0x0263,
    'к': 0x0264,
    'л': 0x0265,
    'м': 0x0266,
    'н': 0x0267,
    'о': 0x0268,
    'п': 0x0269,
    'р': 0x026A,
    'с': 0x026B,
    'т': 0x026C,
    'у': 0x026D,
    'ф': 0x026E,
    'х': 0x026F,
    'ц': 0x0270,
    'ч': 0x0271,
    'я': 0x0279,
    'ё': 0x027A,
    # Рубль кодируется особым образом в Sber PDF
}

# Специальный код для рубля (не стандартный CID)
RUBLE_BYTES = b'\\rZ'  # В Sber PDF рубль = \r + Z (escape sequence)
CID_TO_CHAR = {v: k for k, v in CID.items()}

# Доступные символы в шрифте
AVAILABLE_CHARS = set(CID.keys())
AVAILABLE_CHARS.add('₽')  # Рубль доступен через специальный код

# Доступные кириллические буквы
AVAILABLE_CYRILLIC_UPPER = "АБДИКМНОПРСТФЧЮ"
AVAILABLE_CYRILLIC_LOWER = "абвдеийклмнопрстуфхцчяё"

# НЕДОСТУПНЫЕ кириллические буквы (НЕТ в шаблоне!)
MISSING_UPPER = "ВГЕЁЖЗЙЛУЦШЩЪЫЬЭЯ"
MISSING_LOWER = "гжзшщъыьэю"


def check_text(text: str) -> list:
    """Проверяет текст на недоступные буквы. Возвращает список недоступных."""
    missing = []
    for char in text:
        if char not in AVAILABLE_CHARS and char not in ' \t\n':
            if char not in missing:
                missing.append(char)
    return missing


def get_available_chars_info() -> str:
    """Возвращает информацию о доступных буквах"""
    return f"""
ДОСТУПНЫЕ БУКВЫ В ШРИФТЕ СБЕРБАНКА:
   Заглавные: {AVAILABLE_CYRILLIC_UPPER}
   Строчные:  {AVAILABLE_CYRILLIC_LOWER}
   Латиница:  B
   Цифры:     0-9
   Символы:   . , : - + ( ) •
   
НЕДОСТУПНЫЕ (избегайте в именах):
   Заглавные: {MISSING_UPPER}
   Строчные:  {MISSING_LOWER}
"""


# Ширины символов (приблизительные, для Arial 12pt)
CHAR_WIDTH = {
    ' ': 3.3,
    '(': 4.0,
    ')': 4.0,
    '+': 7.0,
    '-': 4.0,
    '.': 3.3,
    '0': 6.7,
    '1': 6.7,
    '2': 6.7,
    '3': 6.7,
    '4': 6.7,
    '5': 6.7,
    '6': 6.7,
    '7': 6.7,
    '8': 6.7,
    '9': 6.7,
    ':': 3.3,
    'B': 8.0,
    '•': 4.0,
    '₽': 8.0,
    # Кириллица (средние значения)
    'А': 8.0, 'Б': 8.0, 'Д': 8.0, 'И': 8.0, 'К': 8.0,
    'М': 9.3, 'Н': 8.0, 'О': 8.7, 'П': 8.0, 'Р': 7.3,
    'С': 8.0, 'Т': 7.3, 'Ф': 8.7, 'Ч': 8.0, 'Ю': 10.7,
    'а': 6.7, 'б': 6.7, 'в': 6.7, 'д': 6.7, 'е': 6.7,
    'и': 6.7, 'й': 6.7, 'к': 6.0, 'л': 6.7, 'м': 8.0,
    'н': 6.7, 'о': 6.7, 'п': 6.7, 'р': 6.7, 'с': 6.0,
    'т': 5.3, 'у': 6.0, 'ф': 8.7, 'х': 6.0, 'ц': 6.7,
    'ч': 6.7, 'я': 6.7, 'ё': 6.7,
}


def calc_text_width(text: str) -> float:
    """Вычисляет ширину текста в пунктах."""
    return sum(CHAR_WIDTH.get(c, 6.0) for c in text)


def encode_cid(char: str, cid_map: Optional[Dict[str, int]] = None) -> bytes:
    """Кодирует символ в CID (2 байта)."""
    table = cid_map if cid_map is not None else CID
    # Специальная обработка для рубля
    if char == '₽':
        return RUBLE_BYTES
    
    cid = table.get(char, 0x0003)  # Пробел по умолчанию
    high, low = (cid >> 8) & 0xFF, cid & 0xFF
    
    result = b''
    for byte in [high, low]:
        # Экранируем специальные символы PDF: ( ) \
        if byte == 0x28:  # (
            result += b'\\('
        elif byte == 0x29:  # )
            result += b'\\)'
        elif byte == 0x5C:  # \
            result += b'\\\\'
        else:
            result += bytes([byte])
    return result


def encode_text(text: str, cid_map: Optional[Dict[str, int]] = None) -> bytes:
    """Кодирует текст в CID последовательность для Tj."""
    result = b''
    for char in text:
        result += encode_cid(char, cid_map=cid_map)
    return result


def check_text_with_map(text: str, cid_map: Dict[str, int]) -> list:
    """Проверяет текст на символы, отсутствующие в cid_map."""
    missing = []
    for char in text:
        if char in cid_map or char in ' \t\n':
            continue
        if char not in missing:
            missing.append(char)
    return missing


def decode_cid_text(cid_bytes: bytes) -> str:
    """Декодирует CID последовательность в текст."""
    text = ""
    i = 0
    while i < len(cid_bytes):
        # Проверяем на рубль (\rZ)
        if cid_bytes[i:i+3] == b'\\rZ':
            text += '₽'
            i += 3
            continue
        
        # Обработка escape-последовательностей
        if cid_bytes[i:i+1] == b'\\' and i + 1 < len(cid_bytes):
            high = cid_bytes[i+1]
            i += 2
        else:
            high = cid_bytes[i]
            i += 1
        
        if i >= len(cid_bytes):
            break
        
        if cid_bytes[i:i+1] == b'\\' and i + 1 < len(cid_bytes):
            low = cid_bytes[i+1]
            i += 2
        else:
            low = cid_bytes[i]
            i += 1
        
        cid = (high << 8) | low
        text += CID_TO_CHAR.get(cid, f'[{cid:04X}]')
    
    return text


def rebuild_xref(pdf_bytes: bytes) -> bytes:
    """Полностью пересчитывает xref таблицу (LF и CRLF доноры)."""
    objects = []
    for match in re.finditer(rb'(\d+)\s+0\s+obj', pdf_bytes):
        objects.append((int(match.group(1)), match.start()))
    objects.sort(key=lambda x: x[0])

    # Сбер-оригиналы: ``\r\nxref\r\n`` — старый поиск ``xref\n`` ломался.
    xref_start = -1
    for m in re.finditer(rb'[\r\n]xref[\r\n]', pdf_bytes):
        xref_start = m.start() + 1  # на 'x'
    if xref_start < 0:
        m = re.search(rb'(?<![A-Za-z])xref[\r\n]', pdf_bytes)
        if m:
            xref_start = m.start()
    if xref_start < 0 or pdf_bytes[xref_start:xref_start + 4] != b'xref':
        logger.error("Cannot find xref")
        return pdf_bytes

    nl = b'\r\n' if pdf_bytes[xref_start:xref_start + 6].startswith(b'xref\r') else b'\n'

    trailer_start = pdf_bytes.find(b'trailer', xref_start)
    if trailer_start == -1:
        logger.error("Cannot find trailer")
        return pdf_bytes

    eof_pos = pdf_bytes.find(b'%%EOF', trailer_start)
    if eof_pos == -1:
        logger.error("Cannot find %%EOF")
        return pdf_bytes
    trailer_section = pdf_bytes[trailer_start:eof_pos]

    max_obj = max(o[0] for o in objects) if objects else 0
    # Как у Jasper/Сбера: без хвостового пробела перед EOL.
    free = b'0000000000 65535 f' + nl
    xref_lines = [b'xref' + nl, f'0 {max_obj + 1}'.encode() + nl, free]
    obj_dict = {o[0]: o[1] for o in objects}
    for i in range(1, max_obj + 1):
        if i in obj_dict:
            xref_lines.append(f'{obj_dict[i]:010d} 00000 n'.encode() + nl)
        else:
            xref_lines.append(free)

    new_xref = b''.join(xref_lines)
    new_pdf = pdf_bytes[:xref_start] + new_xref + trailer_section
    startxref_match = re.search(rb'startxref\s*[\r\n]+\d+', new_pdf)
    if startxref_match:
        new_pdf = (
            new_pdf[:startxref_match.start()]
            + b'startxref' + nl
            + str(xref_start).encode()
            + nl
            + b'%%EOF'
            + nl
        )
    return new_pdf


DEFAULT_SBP_FIELD_COORDS = {
    'date_time': (711.74, 63.37),
    'receiver_name': (604.74, 21.0),
    'receiver_phone': (565.74, 21.0),
    'recipient_bank': (524.74, 21.0),
    'sender_name': (475.74, 21.0),
    'sender_account': (434.74, 21.0),
    'amount': (385.74, 21.0),
    'commission': (345.74, 21.0),
    'spb_number': (304.74, 21.0),
}

PHONE_FIELD_COORDS = {
    'date_time': (615.74, 75.47),
    'receiver_name': (524.74, 21.0),
    'receiver_phone': (490.74, 21.0),
    'receiver_account': (456.74, 21.0),
    'sender_name': (398.74, 21.0),
    'sender_account': (364.74, 21.0),
    'amount': (330.74, 21.0),
    'commission': (296.74, 21.0),
    'document_num': (238.74, 21.0),
    'auth_code': (204.74, 21.0),
}


def create_sber_stealth(template_path: str = None, data: Dict = None, auto_select: bool = True, cid_map: Optional[Dict[str, int]] = None, field_coords: Optional[Dict[str, Tuple[float, float]]] = None) -> Optional[bytes]:
    """
    Создаёт Sberbank PDF с модифицированными данными.
    
    Args:
        template_path: Путь к шаблону (если None и auto_select=True - выберет автоматически)
        data: Словарь с данными
        auto_select: Автоматически выбирать шаблон с нужными буквами
    
    Returns:
        bytes: PDF файл или None при ошибке
    """
    
    # Сначала проверяем на недоступные символы (дата — через _date_variants при подгонке слота)
    is_phone_layout = bool(field_coords and "document_num" in field_coords)
    if is_phone_layout:
        check_keys = [
            k for k in field_coords
            if k in data and k not in (
                "date_time", "document_num", "auth_code",
                "sender_account", "receiver_account",
            )
        ]
    else:
        check_keys = list(data.keys()) if data else []
    all_text = "".join(str(data[k]) for k in check_keys) if data else ""
    if cid_map is not None:
        missing = check_text_with_map(all_text, cid_map)
    else:
        missing = check_text(all_text)
    if missing:
        logger.error(f"[X] Недоступные символы: {''.join(missing)}")
        logger.error(f"    Добавьте Sber PDF с этими буквами в папку templates/")
        logger.error(f"    Или измените имена, исключив буквы: {MISSING_UPPER}{MISSING_LOWER}")
        return None
    
    # Автоматический выбор шаблона (если template_manager доступен)
    if auto_select and TEMPLATE_MANAGER_AVAILABLE:
        auto_template = find_template_for_data(data)
        
        if auto_template:
            template_path = auto_template
            logger.info(f"[OK] Автоматически выбран шаблон: {template_path}")
        else:
            # Не нашли подходящий шаблон
            missing = get_missing_chars_for_data(data)
            if missing:
                logger.error(f"[X] Нет шаблона с нужными буквами!")
                logger.error(f"    Недостающие символы: {''.join(missing)}")
                logger.error(f"    Добавьте Sber чек с этими буквами в папку templates/")
            return None
    
    if not template_path:
        template_path = "templates/sber_original.pdf"
    
    logger.info(f"SBER STEALTH v3: {template_path}")
    
    # Читаем оригинал
    with open(template_path, 'rb') as f:
        pdf_bytes = bytearray(f.read())
    original_size = len(pdf_bytes)
    
    # Используем fitz для декомпрессии content stream
    doc = fitz.open(template_path)
    
    # Находим content stream (xref 7 в sber_original.pdf)
    contents_xref = None
    for xref in range(1, doc.xref_length()):
        try:
            stream = doc.xref_stream(xref)
            if stream and b'Tm' in stream and b'Tj' in stream and b'BT' in stream:
                contents_xref = xref
                break
        except:
            pass
    
    if not contents_xref:
        logger.error("Content stream not found")
        doc.close()
        return None
    
    decompressed = bytearray(doc.xref_stream(contents_xref))
    orig_decompressed_len = len(decompressed)
    doc.close()
    
    # Находим позицию stream в файле
    obj_marker = f'{contents_xref} 0 obj'.encode()
    obj_start = pdf_bytes.find(obj_marker)
    stream_keyword = pdf_bytes.find(b'stream', obj_start)
    stream_start = pdf_bytes.find(b'\n', stream_keyword) + 1
    endstream_pos = pdf_bytes.find(b'endstream', stream_start)
    stream_end = endstream_pos
    if pdf_bytes[stream_end-1:stream_end] == b'\n':
        stream_end -= 1
    
    original_compressed = bytes(pdf_bytes[stream_start:stream_end])
    logger.info(f"Original: {len(original_compressed)} compressed, {len(decompressed)} decompressed")
    
    field_coords = field_coords or DEFAULT_SBP_FIELD_COORDS
    
    # Паттерн для поиска BT...Tm...(...)Tj...ET блоков
    bt_et_pattern = rb'BT\s*(.*?)\s*ET'
    
    replacements = []
    phone_date_written = None  # type: Optional[str]
    
    for field_name, (target_y, target_x) in field_coords.items():
        if field_name not in data:
            continue
        
        new_value = data[field_name]
        
        # Проверяем на недоступные буквы
        field_missing = check_text_with_map(new_value, cid_map) if cid_map else check_text(new_value)
        if field_missing:
            logger.error(f"[X] {field_name}: недоступные буквы: {''.join(field_missing)}")
            logger.error(f"   Используйте имена без букв: {MISSING_UPPER}{MISSING_LOWER}")
            continue
        
        # Ищем блок с нужной Y координатой
        found = False
        for match in re.finditer(bt_et_pattern, bytes(decompressed), re.DOTALL):
            block = match.group(1)
            block_start = match.start()
            block_end = match.end()
            
            # Ищем Tm в блоке
            tm_match = re.search(rb'1\s+0\s+0\s+1\s+([\d.]+)\s+([\d.]+)\s+Tm', block)
            if not tm_match:
                continue
            
            x = float(tm_match.group(1))
            y = float(tm_match.group(2))
            
            # Проверяем координаты (допуск 2 pt)
            if abs(y - target_y) > 2.0:
                continue
            
            # Ищем (...)Tj в блоке
            tj_match = re.search(rb'\(([^)]*)\)Tj', block)
            if not tj_match:
                continue
            
            old_cid = tj_match.group(1)
            old_text = decode_cid_text(old_cid)

            # Проверяем что текст не совпадает
            if old_text == new_value:
                logger.info(f"[=] {field_name}: без изменений")
                found = True
                if field_name == "date_time":
                    phone_date_written = new_value
                break
            
            # Кодируем новый текст с сохранением длины CID-слота
            old_raw = tj_match.group(1)
            is_phone = bool(field_coords and "document_num" in field_coords)
            from sber_dynamic import (
                _fit_text_encoded_length, _pad_encoded_cids, _splice_ruble_slot, _sber_enc,
            )
            if is_phone:
                _uni_gid = {ord(k): v for k, v in cid_map.items()}
                enc = lambda t, _g=_uni_gid: _sber_enc(t, _g)
            else:
                enc = lambda t, _m=cid_map: encode_text(t, cid_map=_m)

            if field_name in (
                "date_time", "amount", "commission", "receiver_phone",
                "receiver_name", "sender_name",
            ):
                key_hint = field_name
                if field_name == "receiver_phone":
                    key_hint = "phone"
                elif field_name == "date_time":
                    key_hint = "date"
                elif field_name == "receiver_name":
                    key_hint = "receiver"
                elif field_name == "sender_name":
                    key_hint = "sender"
                new_value, new_cid = _fit_text_encoded_length(
                    new_value, len(old_raw), enc, shell_bytes=old_raw,
                    key_hint=key_hint, phone_style=is_phone,
                )
            else:
                new_cid = enc(new_value)
                if len(new_cid) < len(old_raw):
                    padded = _pad_encoded_cids(new_cid, len(old_raw))
                    if padded is not None:
                        new_cid = padded
                elif len(new_cid) > len(old_raw):
                    key_hint = (
                        "receiver" if field_name == "receiver_name"
                        else "sender" if field_name == "sender_name"
                        else field_name
                    )
                    new_value, new_cid = _fit_text_encoded_length(
                        new_value, len(old_raw), enc, shell_bytes=old_raw,
                        key_hint=key_hint, phone_style=is_phone,
                    )
            if field_name in ("amount", "commission"):
                from sber_dynamic import _splice_amount_slot
                spliced = _splice_amount_slot(old_raw, new_value, enc, phone_style=is_phone)
                if spliced == old_raw:
                    # Слот слишком узкий / splice не вписал сумму — не лжём в лог.
                    logger.warning(
                        "[!] %s: splice kept shell amount (need wider slot for %r)",
                        field_name, new_value[:24],
                    )
                    continue
                new_cid = spliced
                if len(new_cid) != len(old_raw):
                    new_cid = _splice_ruble_slot(old_raw, new_cid)
            if len(new_cid) != len(old_raw):
                logger.warning(
                    f"[!] {field_name}: CID length {len(new_cid)} != slot {len(old_raw)}"
                )
                continue
            
            # Находим точную позицию (...)Tj в блоке
            tj_full_match = re.search(rb'\([^)]*\)Tj', block)
            if tj_full_match:
                # Позиция начала ( относительно начала BT блока
                paren_start = match.start(1) + tj_full_match.start() + 1  # +1 для (
                paren_end = match.start(1) + tj_full_match.end() - 3  # -3 для )Tj
                
                replacements.append((paren_start, paren_end, new_cid, field_name, old_text, new_value))
                logger.info(f"[OK] {field_name}: '{old_text[:20]}' -> '{new_value[:20]}' (Y={y})")
                found = True
                if field_name == "date_time":
                    phone_date_written = new_value
                break
        
        if not found:
            logger.warning(f"[!] {field_name}: НЕ НАЙДЕН (target_y={target_y})")
    
    if not replacements and not phone_date_written:
        logger.info("No changes - returning original")
        return bytes(pdf_bytes)
    
    # Применяем замены (в обратном порядке чтобы не сбить позиции)
    replacements.sort(key=lambda r: r[0], reverse=True)
    for start, end, new_bytes, field_name, old_text, new_text in replacements:
        decompressed = decompressed[:start] + bytearray(new_bytes) + decompressed[end:]

    # Сбер: дата по центру X=153 — только если skeleton не уезжает
    # (иначе SBER_CONTENT_SKELETON_DRIFT → Proton ФЕЙК).
    if phone_date_written:
        from sber_dynamic import (
            _recenter_phone_date_stream,
            _recenter_sber_date_stream,
            _content_skeleton_hash,
            SBP_DATE_Y,
        )
        before = bytes(decompressed)
        sk0 = _content_skeleton_hash(before)
        if is_phone_layout:
            after = _recenter_phone_date_stream(
                before,
                phone_date_written,
                template_path=template_path,
            )
        else:
            after = _recenter_sber_date_stream(
                before,
                phone_date_written,
                SBP_DATE_Y,
                template_path=template_path,
            )
        # Только если skeleton (Tm) не ушёл из atlas — иначе Proton НЕИЗВЕСТНЫЙ.
        if (
            len(after) == len(before)
            and _content_skeleton_hash(after) == sk0
        ):
            decompressed = bytearray(after)
            logger.info("Sber date recentered for %r", phone_date_written[:40])
        else:
            logger.warning(
                "Sber date recenter skipped (skeleton/len drift) for %r",
                phone_date_written[:40],
            )
    
    logger.info(f"Applied {len(replacements)} changes, stream: {len(decompressed)} bytes")

    from openpdf_deflate import compress_to_size, openpdf_deflate

    target_clen = len(original_compressed)
    raw = bytes(decompressed)

    def _try_exact(payload: bytes) -> Optional[bytes]:
        """Только zlib header 78 9c (как у оригиналов Сбера) и exact Length."""
        if len(payload) != orig_decompressed_len:
            return None

        def _ok(c: Optional[bytes]) -> Optional[bytes]:
            # Proton rstrip(\\r\\n) before zlib — flate must not end with CR/LF.
            # Decoded length must stay exact: compress_to_size/nudge pad → OnlyPDF FAKE.
            if (
                c is None
                or c[:2] != b"\x78\x9c"
                or len(c) != target_clen
                or c.endswith((b"\r", b"\n"))
            ):
                return None
            try:
                if len(zlib.decompress(c)) != orig_decompressed_len:
                    return None
            except Exception:
                return None
            return c

        for level in (6, 5, 4, 3, 2, 1, 9, 8, 7):
            hit = _ok(zlib.compress(payload, level))
            if hit is not None:
                return hit
        for level in (6, 5, 4, 3):
            hit = _ok(openpdf_deflate(payload, level))
            if hit is not None:
                return hit
        # compress_to_size / nudge меняют decoded length → OnlyPDF FAKE — не используем.
        return None

    new_compressed = _try_exact(raw)

    if new_compressed is None or len(new_compressed) != target_clen:
        logger.error(
            "Sber stealth: exact flate miss (decoded=%d target=%d) — abort "
            "(no Length/xref; keeps Proton originals + gens off FAKE)",
            len(raw),
            target_clen,
        )
        return None

    pdf_bytes = pdf_bytes[:stream_start] + bytearray(new_compressed) + pdf_bytes[stream_end:]
    result = bytes(pdf_bytes)
    if len(result) != original_size:
        logger.error(
            "Sber stealth: size drift %d→%d — abort",
            original_size,
            len(result),
        )
        return None

    # Нельзя отдавать битый startxref — Proton пометит даже «почти оригинал».
    sx = re.search(rb"startxref\s*[\r\n]+(\d+)", result)
    if not sx or result[int(sx.group(1)) : int(sx.group(1)) + 4] != b"xref":
        logger.error("Sber stealth: startxref broken — abort")
        return None

    logger.info(f"RESULT: {original_size} -> {len(result)} ({len(result)-original_size:+d})")
    return result


if __name__ == "__main__":
    # Тестовые данные
    data = {
        'date_time': "23 декабря 2025 10:16:30 (МСК)",
        'amount': "30000.00  ₽",
        'commission': "0.00  ₽",
        'sender_name': "Андрей Розенталь О.",
        'receiver_name': "Александр Пушкин М",
        'receiver_phone': "+7 917 962-84-59",
        'recipient_bank': "Т-Банк",
        'sender_account': "•••• 1234",
        'spb_number': "B1234567890123456789012345678943",
    }
    
    # Проверяем данные на недоступные символы
    all_text = ''.join(str(v) for v in data.values())
    missing = check_text(all_text)
    if missing:
        logger.error(f"[X] Недоступные символы в данных: {''.join(missing)}")
        logger.info("    Измените данные, используя только доступные буквы")
        logger.info(get_available_chars_info())
    else:
        # Создаём PDF
        result = create_sber_stealth(data=data, auto_select=False)
        
        if result:
            with open("test_sber_v3.pdf", "wb") as f:
                f.write(result)
            logger.info(f"[OK] Сохранено: test_sber_v3.pdf ({len(result)} bytes)")
        else:
            logger.error("[X] Не удалось создать PDF")
