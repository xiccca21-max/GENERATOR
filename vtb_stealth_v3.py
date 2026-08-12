"""
VTB STEALTH v3 - Чистая байтовая замена с пересчётом xref
Автоматически выбирает шаблон с нужными буквами.
"""

import fitz
import zlib
import re
import logging
from typing import Dict, Optional, List, Tuple

# Импорт менеджера шаблонов
try:
    from template_manager import find_template_for_data, get_missing_chars_for_data, get_templates_info
    TEMPLATE_MANAGER_AVAILABLE = True
except ImportError:
    TEMPLATE_MANAGER_AVAILABLE = False

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(levelname)s | %(message)s')

# CID маппинг (только символы из шрифта PDF!)
CID = {
    ' ': 0x0003,
    '0': 0x0013, '1': 0x0014, '2': 0x0015, '3': 0x0016, '4': 0x0017,
    '5': 0x0018, '6': 0x0019, '7': 0x001A, '8': 0x001B, '9': 0x001C,
    '.': 0x0011, ',': 0x000F, ':': 0x001D,
    '(': 0x000B, ')': 0x000C,
    '+': 0x000E, '-': 0x0010, '\u2011': 0x0010,
    # Латиница (только A, D, I, Y есть в шрифте!)
    'A': 0x0024, 'D': 0x0027, 'I': 0x002C, 'Y': 0x003C,
    '₽': 0x0440,
    'А': 0x021C, 'Б': 0x021D, 'В': 0x021E, 'Д': 0x0220,
    'И': 0x0224, 'К': 0x0226, 'М': 0x0228, 'О': 0x022A,
    'П': 0x022B, 'Р': 0x022C, 'С': 0x022D, 'Т': 0x022E,
    'а': 0x023C, 'б': 0x023D, 'в': 0x023E, 'г': 0x023F,
    'д': 0x0240, 'е': 0x0241, 'и': 0x0244, 'й': 0x0245,
    'к': 0x0246, 'л': 0x0247, 'м': 0x0248, 'н': 0x0249,
    'о': 0x024A, 'п': 0x024B, 'р': 0x024C, 'с': 0x024D,
    'т': 0x024E, 'у': 0x024F, 'ф': 0x0250, 'х': 0x0251,
    'ц': 0x0252, 'ч': 0x0253, 'щ': 0x0255, 'ы': 0x0257,
    'ь': 0x0258, 'я': 0x025B,
}
CID_TO_CHAR = {v: k for k, v in CID.items()}

# Доступные символы в шрифте
AVAILABLE_CHARS = set(CID.keys())

# Доступные кириллические буквы
AVAILABLE_CYRILLIC_UPPER = "АБВДИКМОПРСТ"
AVAILABLE_CYRILLIC_LOWER = "абвгдейклмнопрстуфхцчщыья"

# Недоступные кириллические буквы
MISSING_UPPER = "ГЕЁЖЗЙЛНУФХЦЧШЩЪЫЬЭЮЯ"
MISSING_LOWER = "ёжзшэю"

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
📝 ДОСТУПНЫЕ БУКВЫ В ШРИФТЕ:
   Заглавные: {AVAILABLE_CYRILLIC_UPPER}
   Строчные:  {AVAILABLE_CYRILLIC_LOWER}
   Латиница:  A D I Y
   Цифры:     0-9
   Символы:   . , : - + ( ) * ₽
   
❌ НЕДОСТУПНЫЕ (избегайте в именах):
   Заглавные: {MISSING_UPPER}
   Строчные:  {MISSING_LOWER}
"""

# ТОЧНЫЕ ширины символов из PDF шрифта (SF Pro Display @ 12pt)
# Извлечены из /W массива CIDFont
CHAR_WIDTH = {
    ' ': 2.46,   # CID 0003, 205/1000 em
    '(': 3.86,   # CID 000B
    ')': 3.86,   # CID 000C
    '*': 5.78,   # CID 000D, 482/1000 em
    '+': 7.26,   # CID 000E
    ',': 3.05,   # CID 000F
    '-': 5.15,   # CID 0010
    '\u2011': 5.15,  # Non-breaking hyphen = same as -
    '.': 3.05,   # CID 0011
    '0': 7.27,   # CID 0013
    '1': 5.32,   # CID 0014
    '2': 6.79,   # CID 0015
    '3': 7.10,   # CID 0016
    '4': 7.26,   # CID 0017
    '5': 7.02,   # CID 0018
    '6': 7.19,   # CID 0019
    '7': 6.56,   # CID 001A
    '8': 7.19,   # CID 001B
    '9': 7.19,   # CID 001C
    ':': 3.05,   # CID 001D
    'A': 7.57,   # CID 0024
    'D': 8.14,   # CID 0027
    'I': 2.70,   # CID 002C
    'Y': 7.34,   # CID 003C
    '₽': 7.26,   # CID 0440
    # Кириллица заглавная
    'А': 7.69,   # CID 021C
    'Б': 7.37,   # CID 021D
    'В': 7.37,   # CID 021E
    'Д': 8.68,   # CID 0220
    'И': 8.51,   # CID 0224
    'К': 7.46,   # CID 0226
    'М': 10.09,  # CID 0228
    'О': 8.89,   # CID 022A
    'П': 8.39,   # CID 022B
    'Р': 7.04,   # CID 022C
    'С': 8.33,   # CID 022D
    'Т': 7.09,   # CID 022E
    # Кириллица строчная
    'а': 6.13,   # CID 023C
    'б': 6.60,   # CID 023D
    'в': 5.88,   # CID 023E
    'г': 5.03,   # CID 023F
    'д': 6.94,   # CID 0240
    'е': 6.25,   # CID 0241
    'и': 6.54,   # CID 0244
    'й': 6.54,   # CID 0245
    'к': 5.84,   # CID 0246
    'л': 6.34,   # CID 0247
    'м': 8.26,   # CID 0248
    'н': 6.46,   # CID 0249
    'о': 6.48,   # CID 024A
    'п': 6.40,   # CID 024B
    'р': 6.71,   # CID 024C
    'с': 6.11,   # CID 024D
    'т': 5.40,   # CID 024E
    'у': 5.94,   # CID 024F
    'ф': 8.12,   # CID 0250
    'х': 5.75,   # CID 0251
    'ц': 6.79,   # CID 0252
    'ч': 6.07,   # CID 0253
    'щ': 9.40,   # CID 0255
    'ы': 7.66,   # CID 0257
    'ь': 5.56,   # CID 0258
    'я': 5.80,   # CID 025B
}

def calc_text_width(text: str, kerning: float = -16.66667) -> float:
    """Вычисляет ширину текста в пунктах с учётом kerning.
    
    Коэффициент 0.79 вычислен из реальных данных:
    - "*1815" реальная ширина = 23.6pt, вычисленная = 29.83pt
    - Коэффициент = 23.6 / 29.83 = 0.79
    """
    if not text:
        return 0.0
    
    # Сумма ширин символов
    char_widths = sum(CHAR_WIDTH.get(c, 6.0) for c in text)
    
    # Kerning применяется между символами (n-1 раз)
    kerning_pt = kerning * 12 / 1000  # конвертируем в пункты
    total_kerning = kerning_pt * (len(text) - 1)
    
    # Применяем корректирующий коэффициент 0.79
    raw_width = char_widths + total_kerning
    return raw_width * 0.79


def encode_cid(char: str) -> bytes:
    if char == '-':
        char = '\u2011'
    cid = CID.get(char, 0x0003)
    high, low = (cid >> 8) & 0xFF, cid & 0xFF
    result = b''
    for byte in [high, low]:
        if byte in (0x28, 0x29, 0x5C):
            result += b'\\' + bytes([byte])
        else:
            result += bytes([byte])
    return result


def decode_tj_text(tj_block: bytes) -> str:
    text = ""
    i = 0
    while i < len(tj_block):
        if tj_block[i:i+1] == b'(':
            i += 1
            while i < len(tj_block) and tj_block[i:i+1] != b')':
                if tj_block[i:i+1] == b'\\' and i + 1 < len(tj_block):
                    high = tj_block[i+1]
                    i += 2
                else:
                    high = tj_block[i]
                    i += 1
                if i >= len(tj_block) or tj_block[i:i+1] == b')':
                    break
                if tj_block[i:i+1] == b'\\' and i + 1 < len(tj_block):
                    low = tj_block[i+1]
                    i += 2
                else:
                    low = tj_block[i]
                    i += 1
                cid = (high << 8) | low
                text += CID_TO_CHAR.get(cid, '?')
        else:
            i += 1
    return text


def rebuild_xref(pdf_bytes: bytes) -> bytes:
    """Полностью пересчитывает xref таблицу."""
    # Находим все объекты
    obj_pattern = rb'(\d+)\s+0\s+obj'
    objects = []
    for match in re.finditer(obj_pattern, pdf_bytes):
        obj_num = int(match.group(1))
        offset = match.start()
        objects.append((obj_num, offset))
    
    objects.sort(key=lambda x: x[0])
    
    # Находим старую xref таблицу
    xref_start = pdf_bytes.rfind(b'\nxref\n')
    if xref_start == -1:
        xref_start = pdf_bytes.find(b'xref\n')
    else:
        xref_start += 1
    
    if xref_start == -1:
        logger.error("Cannot find xref")
        return pdf_bytes
    
    # Находим trailer
    trailer_start = pdf_bytes.find(b'trailer', xref_start)
    if trailer_start == -1:
        logger.error("Cannot find trailer")
        return pdf_bytes
    
    # Получаем trailer content
    eof_pos = pdf_bytes.find(b'%%EOF', trailer_start)
    trailer_section = pdf_bytes[trailer_start:eof_pos]
    
    # Строим новую xref таблицу
    max_obj = max(o[0] for o in objects) if objects else 0
    
    xref_lines = [b'xref\n']
    xref_lines.append(f'0 {max_obj + 1}\n'.encode())
    xref_lines.append(b'0000000000 65535 f \n')  # Object 0
    
    obj_dict = {o[0]: o[1] for o in objects}
    for i in range(1, max_obj + 1):
        if i in obj_dict:
            offset = obj_dict[i]
            xref_lines.append(f'{offset:010d} 00000 n \n'.encode())
        else:
            xref_lines.append(b'0000000000 65535 f \n')
    
    new_xref = b''.join(xref_lines)
    
    # Собираем новый PDF
    new_pdf = pdf_bytes[:xref_start] + new_xref + trailer_section
    
    # Обновляем startxref
    new_xref_pos = xref_start
    startxref_match = re.search(rb'startxref\s*\n\d+', new_pdf)
    if startxref_match:
        new_pdf = new_pdf[:startxref_match.start()] + f'startxref\n{new_xref_pos}'.encode() + b'\n%%EOF\n'
    
    return new_pdf


def create_vtb_stealth(template_path: str = None, data: Dict = None, auto_select: bool = True) -> Optional[bytes]:
    """
    Создаёт VTB PDF с модифицированными данными.
    
    Args:
        template_path: Путь к шаблону (если None и auto_select=True - выберет автоматически)
        data: Словарь с данными
        auto_select: Автоматически выбирать шаблон с нужными буквами
    
    Returns:
        bytes: PDF файл или None при ошибке
    """
    
    # Автоматический выбор шаблона
    if auto_select and TEMPLATE_MANAGER_AVAILABLE:
        auto_template = find_template_for_data(data)
        
        if auto_template:
            template_path = auto_template
            logger.info(f"🔍 Автоматически выбран шаблон: {template_path}")
        else:
            # Не нашли подходящий шаблон
            missing = get_missing_chars_for_data(data)
            if missing:
                logger.error(f"❌ Нет шаблона с нужными буквами!")
                logger.error(f"   Недостающие символы: {''.join(missing)}")
                logger.error(f"   Добавьте VTB чек с этими буквами в папку templates/")
            return None
    
    if not template_path:
        template_path = "templates/original_vtb.pdf"
    
    logger.info(f"🥷 VTB STEALTH v3: {template_path}")
    
    # Читаем оригинал
    with open(template_path, 'rb') as f:
        pdf_bytes = bytearray(f.read())
    original_size = len(pdf_bytes)
    
    # Используем fitz для декомпрессии content stream
    doc = fitz.open(template_path)
    
    contents_xref = None
    for xref in range(1, doc.xref_length()):
        try:
            stream = doc.xref_stream(xref)
            if stream and b'Tm' in stream:
                contents_xref = xref
                break
        except:
            pass
    
    if not contents_xref:
        doc.close()
        return None
    
    decompressed = bytearray(doc.xref_stream(contents_xref))
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
    
    # Парсим блоки
    tm_tj_pattern = rb'([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+Tm\s*\n?(\[[^\]]*\])\s*TJ'
    matches = list(re.finditer(tm_tj_pattern, bytes(decompressed)))
    
    # Координаты полей: (target_y, min_x)
    # receiver_header находится в заголовке (Y ~327 для original_vtb, может отличаться)
    field_coords = {
        'date_time': (275.2, 150),
        'amount': (72.4, 150),
        'sender': (227.2, 100),
        'receiver': (203.2, 100),
        'phone': (179.2, 100),
        'spb': (131.2, 100),
        'recipient_bank': (155.2, 150),
    }
    
    def extract_kerning(tj: bytes) -> bytes:
        m = re.search(rb'\)(-[\d.]+)', tj)
        return m.group(1) if m else b'-16.66667'
    
    replacements = []
    
    # Отдельная обработка receiver_header - ищем вторую строку заголовка (Y ~327)
    # Первая строка "Исходящий перевод СБП" имеет Y ~345, вторая с именем Y ~327
    if 'receiver_header' in data:
        new_value = data['receiver_header']
        missing = check_text(new_value)
        if not missing:
            for match in matches:
                y = float(match.group(6).decode())
                x = float(match.group(5).decode())
                # Вторая строка заголовка: Y между 320-340, X ~82
                if 320 < y < 340 and 75 < x < 95:
                    old_tj = match.group(7)
                    old_text = decode_tj_text(old_tj).replace('\u2011', '-')
                    
                    if old_text == new_value.replace('\u2011', '-'):
                        logger.info(f"⏭️ receiver_header: unchanged")
                        break
                    
                    kerning_bytes = extract_kerning(old_tj)
                    new_x = x  # Сохраняем оригинальный X (левое выравнивание)
                    
                    new_tj_parts = []
                    for i, char in enumerate(new_value):
                        new_tj_parts.append(b'(' + encode_cid(char) + b')')
                        if i < len(new_value) - 1:
                            new_tj_parts.append(kerning_bytes + b' ')
                    new_tj = b''.join(new_tj_parts)
                    
                    a, b, c, d = match.group(1).decode(), match.group(2).decode(), match.group(3).decode(), match.group(4).decode()
                    new_tm = f'{a} {b} {c} {d} {new_x:.5g} {y:.5g} Tm\n'.encode()
                    full_new = new_tm + b'[' + new_tj + b'] TJ'
                    
                    replacements.append((match.start(), match.end(), full_new, 'receiver_header'))
                    logger.info(f"✅ receiver_header: '{old_text[:15]}' -> '{new_value[:15]}' (Y={y})")
                    break
    
    for field_name, (target_y, min_x) in field_coords.items():
        if field_name not in data:
            continue
        new_value = data[field_name]
        
        # Проверяем на недоступные буквы
        missing = check_text(new_value)
        if missing:
            logger.error(f"❌ {field_name}: недоступные буквы: {''.join(missing)}")
            logger.error(f"   Используйте имена без букв: {MISSING_UPPER}{MISSING_LOWER}")
            continue
        
        found = False
        for match in matches:
            y = float(match.group(6).decode())
            x = float(match.group(5).decode())
            
            if abs(y - target_y) < 3.0 and x >= min_x:
                old_tj = match.group(7)
                old_text = decode_tj_text(old_tj).replace('\u2011', '-')
                new_normalized = new_value.replace('\u2011', '-')
                
                if old_text == new_normalized:
                    logger.info(f"⏭️ {field_name}: unchanged")
                    break
                
                kerning_bytes = extract_kerning(old_tj)
                kerning_val = float(kerning_bytes.decode())  # Конвертируем в число
                
                # Для заголовка - левый край как в оригинале
                if field_name == 'receiver_header':
                    new_x = x  # Оригинальная X координата
                elif field_name == 'amount':
                    # Считаем цифры в сумме
                    digits = sum(1 for c in new_value if c.isdigit())
                    if digits >= 6:
                        # 6+ значная сумма (100000+) - X как в 100.pdf
                        new_x = 195.3
                        logger.info(f"   amount: {digits} цифр, X=195.3 (6+ digits)")
                    elif digits >= 5:
                        # 5-значная сумма (10000-99999) - X как в 213145.pdf
                        new_x = 203.7
                        logger.info(f"   amount: {digits} цифр, X=203.7 (5 digits)")
                    else:
                        # 4-значная и меньше - X как в original_vtb
                        new_x = 210.0
                        logger.info(f"   amount: {digits} цифр, X=210.0 (4 digits)")
                else:
                    # Остальные поля - правый край = 257.1
                    RIGHT_EDGE = 257.1
                    new_width = calc_text_width(new_value, kerning_val)
                    new_x = RIGHT_EDGE - new_width
                    
                    # Защита от выхода за левый край
                    if new_x < 100:
                        while new_x < 100 and len(new_value) > 5:
                            new_value = new_value[:-2] + "."
                            new_width = calc_text_width(new_value, kerning_val)
                            new_x = RIGHT_EDGE - new_width
                
                new_tj_parts = []
                for i, char in enumerate(new_value):
                    new_tj_parts.append(b'(' + encode_cid(char) + b')')
                    if i < len(new_value) - 1:
                        new_tj_parts.append(kerning_bytes + b' ')
                new_tj = b''.join(new_tj_parts)
                
                # Получаем параметры Tm и строим новый с корректированной X
                a, b, c, d = match.group(1).decode(), match.group(2).decode(), match.group(3).decode(), match.group(4).decode()
                new_tm = f'{a} {b} {c} {d} {new_x:.5g} {y:.5g} Tm\n'.encode()
                full_new = new_tm + b'[' + new_tj + b'] TJ'
                
                replacements.append((match.start(), match.end(), full_new, field_name))
                logger.info(f"✅ {field_name}: '{old_text[:15]}' -> '{new_value[:15]}'")
                found = True
                break
        
        if not found:
            logger.warning(f"⚠️ {field_name}: НЕ НАЙДЕН (target_y={target_y}, min_x={min_x})")
    
    if not replacements:
        logger.info("No changes - returning original")
        return bytes(pdf_bytes)
    
    # Применяем замены
    replacements.sort(key=lambda r: r[0], reverse=True)
    for start, end, new_bytes, _ in replacements:
        decompressed = decompressed[:start] + bytearray(new_bytes) + decompressed[end:]
    
    logger.info(f"Applied {len(replacements)} changes, stream: {len(decompressed)} bytes")
    
    # Сжимаем с уровнем который даёт размер <= оригинала
    best = None
    for level in range(1, 10):
        c = zlib.compress(bytes(decompressed), level)
        if best is None or len(c) < len(best):
            best = c
        if len(c) <= len(original_compressed):
            best = c
            break
    
    new_compressed = best
    size_diff = len(new_compressed) - len(original_compressed)
    logger.info(f"Compression: {len(original_compressed)} -> {len(new_compressed)} ({size_diff:+d})")
    
    # Обновляем /Length
    length_pattern = rb'/Length\s+(\d+)'
    length_match = re.search(length_pattern, pdf_bytes[obj_start:stream_keyword])
    if length_match:
        old_len_str = length_match.group(1)
        new_len_str = str(len(new_compressed)).encode()
        
        # Паддим если нужно
        if len(new_len_str) < len(old_len_str):
            new_len_str = new_len_str + b' ' * (len(old_len_str) - len(new_len_str))
        
        len_pos = obj_start + length_match.start(1)
        pdf_bytes[len_pos:len_pos + len(old_len_str)] = new_len_str[:len(old_len_str)]
    
    # Заменяем stream
    pdf_bytes = pdf_bytes[:stream_start] + bytearray(new_compressed) + pdf_bytes[stream_end:]
    
    # Пересчитываем xref
    result = rebuild_xref(bytes(pdf_bytes))
    
    logger.info(f"🥷 RESULT: {original_size} -> {len(result)} ({len(result)-original_size:+d})")
    return result


if __name__ == "__main__":
    # Тестовые данные
    receiver_name = "  Айнур Радикович А."
    data = {
        'date_time': "22.01.2026, 20:06",
        'amount': "666 666 ₽",
        'sender': "Данил Сергеевич Д.",
        'receiver': receiver_name,
        'receiver_header': receiver_name,
        'phone': "+7 (999) 999-99-19",
        'recipient_bank': "Сбербанк",  # Без букв Г, з, б
        'spb': "A60141634375560Y000001004",
    }
    
    # auto_select=True - автоматически выберет шаблон с нужными буквами
    result = create_vtb_stealth(data=data, auto_select=True)
    
    if result:
        with open("test_v3_new.pdf", "wb") as f:
            f.write(result)
        print(f"✅ Saved: test_v3_new.pdf ({len(result)} bytes)")
    else:
        print("❌ Не удалось создать PDF")
        print("\nПроверьте:")
        print("1. Есть ли PDF файлы в папке templates/")
        print("2. Запустите scan_templates.bat для сканирования")
