"""
OZON STEALTH v3 - Простой и чистый подход
Использует шаблоны с полным шрифтом и правильный CID маппинг.
"""

import fitz
import zlib
import re
import os
import logging
from typing import Dict, Optional, Set

# Импорт менеджера шаблонов
try:
    from template_manager_ozon import find_template_for_data, get_missing_chars_for_data, get_templates_info
    TEMPLATE_MANAGER_AVAILABLE = True
except ImportError:
    TEMPLATE_MANAGER_AVAILABLE = False

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(levelname)s | %(message)s')


def extract_cid_map(pdf_path: str) -> Dict[str, int]:
    """Извлекает CID маппинг из ToUnicode потока PDF."""
    cid_map = {' ': 0x0003}
    
    try:
        doc = fitz.open(pdf_path)
        
        for xref in range(1, doc.xref_length()):
            try:
                stream = doc.xref_stream_raw(xref)
                if not stream:
                    continue
                
                try:
                    stream = zlib.decompress(stream)
                except:
                    pass
                
                if b'beginbfchar' not in stream and b'beginbfrange' not in stream:
                    continue
                
                content = stream.decode('latin-1', errors='ignore')
                
                # bfchar: <CID> <Unicode>
                for match in re.finditer(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>(?!\s*<)', content):
                    try:
                        cid = int(match.group(1), 16)
                        unicode_val = int(match.group(2), 16)
                        char = chr(unicode_val)
                        cid_map[char] = cid
                    except:
                        pass
                
                # bfrange: <start> <end> <unicode_start>
                for match in re.finditer(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', content):
                    cid_start = int(match.group(1), 16)
                    cid_end = int(match.group(2), 16)
                    unicode_start = int(match.group(3), 16)
                    
                    for i in range(cid_end - cid_start + 1):
                        try:
                            char = chr(unicode_start + i)
                            cid_map[char] = cid_start + i
                        except:
                            pass
                
            except:
                pass
        
        doc.close()
    except Exception as e:
        logger.error(f"Error extracting CID map: {e}")
    
    return cid_map


def check_text(text: str, cid_map: Dict[str, int]) -> list:
    """Проверяет текст на недоступные символы."""
    missing = []
    for char in text:
        if char not in cid_map and char not in ' \t\n':
            if char not in missing:
                missing.append(char)
    return missing


def rebuild_xref(pdf_bytes: bytes) -> bytes:
    """Пересчитывает xref таблицу."""
    obj_pattern = rb'(\d+)\s+0\s+obj'
    objects = []
    for match in re.finditer(obj_pattern, pdf_bytes):
        obj_num = int(match.group(1))
        offset = match.start()
        objects.append((obj_num, offset))
    
    objects.sort(key=lambda x: x[0])
    
    xref_start = pdf_bytes.rfind(b'\nxref\n')
    if xref_start == -1:
        xref_start = pdf_bytes.find(b'xref\n')
    else:
        xref_start += 1
    
    if xref_start == -1:
        return pdf_bytes
    
    trailer_start = pdf_bytes.find(b'trailer', xref_start)
    if trailer_start == -1:
        return pdf_bytes
    
    eof_pos = pdf_bytes.find(b'%%EOF', trailer_start)
    trailer_section = pdf_bytes[trailer_start:eof_pos]
    
    max_obj = max(o[0] for o in objects) if objects else 0
    
    xref_lines = [b'xref\n']
    xref_lines.append(f'0 {max_obj + 1}\n'.encode())
    xref_lines.append(b'0000000000 65535 f \n')
    
    obj_dict = {o[0]: o[1] for o in objects}
    for i in range(1, max_obj + 1):
        if i in obj_dict:
            offset = obj_dict[i]
            xref_lines.append(f'{offset:010d} 00000 n \n'.encode())
        else:
            xref_lines.append(b'0000000000 65535 f \n')
    
    new_xref = b''.join(xref_lines)
    new_pdf = pdf_bytes[:xref_start] + new_xref + trailer_section
    
    new_xref_pos = xref_start
    startxref_match = re.search(rb'startxref\s*\n\d+', new_pdf)
    if startxref_match:
        new_pdf = new_pdf[:startxref_match.start()] + f'startxref\n{new_xref_pos}'.encode() + b'\n%%EOF\n'
    
    return new_pdf


def decode_bt_block(block: bytes, cid_map: Dict[str, int]) -> str:
    """Декодирует текст из BT блока."""
    cid_to_char = {v: k for k, v in cid_map.items()}
    text = ""
    
    for match in re.finditer(rb'<([0-9A-Fa-f]+)>\s*Tj', block):
        cid = int(match.group(1).decode(), 16)
        char = cid_to_char.get(cid, '?')
        text += char
    
    return text


def encode_bt_block(text: str, original_block: bytes, cid_map: Dict[str, int]) -> bytes:
    """
    Кодирует текст в BT блок, сохраняя оригинальную структуру Td.
    Заменяет только CID, оставляя Td смещения как есть.
    """
    # Находим все <CID> Tj паттерны
    pattern = rb'<[0-9A-Fa-f]+>\s*Tj'
    matches = list(re.finditer(pattern, original_block))
    
    if len(matches) != len(text):
        # Длины не совпадают - нужно перестроить блок
        return rebuild_bt_block(text, original_block, cid_map)
    
    # Длины совпадают - просто заменяем CID
    result = bytearray(original_block)
    
    # Заменяем в обратном порядке чтобы не сбить позиции
    for i, match in enumerate(reversed(matches)):
        char_idx = len(text) - 1 - i
        char = text[char_idx]
        cid = cid_map.get(char, 0x0003)
        new_cid = f'<{cid:04X}> Tj'.encode()
        result[match.start():match.end()] = new_cid
    
    return bytes(result)


def rebuild_bt_block(text: str, original_block: bytes, cid_map: Dict[str, int]) -> bytes:
    """Полностью перестраивает BT блок с новым текстом."""
    # Извлекаем Tm из оригинала
    tm_match = re.search(rb'([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+Tm', original_block)
    if not tm_match:
        return original_block
    
    # Извлекаем шрифт
    font_match = re.search(rb'/F(\d+)\s+(\d+)\s+Tf', original_block)
    font_str = font_match.group(0).decode() if font_match else '/F4 14 Tf'
    
    # Извлекаем MCID если есть
    mcid_match = re.search(rb'/P\s*<<\s*/MCID\s+(\d+)\s*>>\s*BDC', original_block)
    mcid_str = mcid_match.group(0).decode() if mcid_match else ''
    
    # Извлекаем средний Td из оригинала
    td_matches = list(re.finditer(rb'([\d.-]+)\s+0\s+Td', original_block))
    if td_matches:
        avg_td = sum(float(m.group(1).decode()) for m in td_matches) / len(td_matches)
    else:
        avg_td = 7.0  # Default
    
    # Строим новый блок
    parts = []
    if mcid_str:
        parts.append(mcid_str)
    parts.append(font_str)
    
    tm_str = f'{tm_match.group(1).decode()} {tm_match.group(2).decode()} {tm_match.group(3).decode()} {tm_match.group(4).decode()} {tm_match.group(5).decode()} {tm_match.group(6).decode()} Tm'
    parts.append(tm_str)
    
    # Добавляем символы
    for i, char in enumerate(text):
        cid = cid_map.get(char, 0x0003)
        if i == 0:
            parts.append(f'<{cid:04X}> Tj')
        else:
            parts.append(f'{avg_td:.6f} 0 Td <{cid:04X}> Tj')
    
    if mcid_str:
        parts.append('EMC')
    
    return '\n'.join(parts).encode()


def find_best_template(data: Dict[str, str]) -> Optional[str]:
    """Находит лучший шаблон с полным шрифтом."""
    if not TEMPLATE_MANAGER_AVAILABLE:
        # Fallback - ищем вручную
        templates_dir = "templates"
        for f in os.listdir(templates_dir):
            if f.startswith('ozonbank_document') and f.endswith('.pdf'):
                return os.path.join(templates_dir, f)
        return "templates/ozon_original.pdf"
    
    return find_template_for_data(data)


def create_ozon_stealth(template_path: str = None, data: Dict = None, auto_select: bool = True) -> Optional[bytes]:
    """
    Создаёт Ozon Bank PDF с модифицированными данными.
    """
    
    # Автоматический выбор шаблона с полным шрифтом
    if auto_select:
        auto_template = find_best_template(data) if data else None
        if auto_template:
            template_path = auto_template
            logger.info(f"[OK] Выбран шаблон: {os.path.basename(template_path)}")
        else:
            if TEMPLATE_MANAGER_AVAILABLE and data:
                missing = get_missing_chars_for_data(data)
                if missing:
                    logger.error(f"[X] Недостающие символы: {''.join(missing)}")
            # Fallback на первый ozonbank_document
            for f in os.listdir("templates"):
                if f.startswith('ozonbank_document') and f.endswith('.pdf'):
                    template_path = os.path.join("templates", f)
                    logger.info(f"[OK] Fallback шаблон: {f}")
                    break
    
    if not template_path:
        template_path = "templates/ozon_original.pdf"
    
    logger.info(f"OZON STEALTH v3: {template_path}")
    
    # Извлекаем CID маппинг
    cid_map = extract_cid_map(template_path)
    logger.info(f"CID map: {len(cid_map)} symbols")
    
    # Проверяем что есть кириллица
    cyrillic_count = sum(1 for c in cid_map if '\u0410' <= c <= '\u044F')
    logger.info(f"Cyrillic in font: {cyrillic_count}")
    
    if cyrillic_count < 60:
        logger.warning("Low Cyrillic count - font may be subset!")
    
    # Читаем PDF
    with open(template_path, 'rb') as f:
        pdf_bytes = bytearray(f.read())
    original_size = len(pdf_bytes)
    
    # Находим content stream
    doc = fitz.open(template_path)
    page = doc[0]
    page_xref = page.xref
    page_dict = doc.xref_object(page_xref)
    
    contents_match = re.search(r'/Contents\s+(\d+)\s+0\s+R', page_dict)
    if contents_match:
        contents_xref = int(contents_match.group(1))
    else:
        # Fallback
        contents_xref = None
        for xref in range(1, doc.xref_length()):
            try:
                stream = doc.xref_stream(xref)
                if stream and b'BT' in stream and b'Tm' in stream:
                    contents_xref = xref
                    break
            except:
                pass
    
    if not contents_xref:
        logger.error("Content stream not found!")
        doc.close()
        return None
    
    # Декомпрессируем stream
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
    
    original_compressed_len = stream_end - stream_start
    logger.info(f"Stream: {original_compressed_len} compressed, {len(decompressed)} decompressed")
    
    # Парсим BT...ET блоки
    bt_blocks = list(re.finditer(rb'BT\s*(.*?)\s*ET', bytes(decompressed), re.DOTALL))
    logger.info(f"Found {len(bt_blocks)} BT...ET blocks")
    
    # Координаты полей Ozon (Y координата из Tm)
    # Ozon использует отражённый Y: 1 0 0 -1 X Y Tm
    field_coords = {
        'date_time': (90, 200),      # Y=90, X>200 (справа)
        'amount': (134, 200),         # Y=134, X>200 (Итого справа)
        'receiver_name': (325, 150),  # Y=325, X>150 (Получатель)
        'receiver_phone': (359, 150), # Y=359, X>150 (Телефон)
        'recipient_bank': (393, 150), # Y=393, X>150 (Банк получателя)
        'sender_name': (427, 150),    # Y=427, X>150 (Отправитель)
    }
    
    replacements = []
    
    for field_name, (target_y, min_x) in field_coords.items():
        if field_name not in data:
            continue
        
        new_value = data[field_name]
        
        # Проверяем символы
        missing = check_text(new_value, cid_map)
        if missing:
            logger.error(f"[X] {field_name}: недоступные символы: {''.join(missing)}")
            continue
        
        found = False
        for match in bt_blocks:
            block = match.group(1)
            
            # Извлекаем координаты из Tm
            tm_match = re.search(rb'([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+Tm', block)
            if not tm_match:
                continue
            
            y = float(tm_match.group(6).decode())
            x = float(tm_match.group(5).decode())
            
            if abs(y - target_y) < 5.0 and x >= min_x:
                old_text = decode_bt_block(block, cid_map)
                
                if not old_text:
                    continue
                
                logger.info(f"  Found {field_name} at Y={y:.0f} X={x:.0f}: '{old_text[:20]}...'")
                
                # Кодируем новый текст
                new_block = encode_bt_block(new_value, block, cid_map)
                
                # Полный блок с BT...ET
                full_old = b'BT\n' + block + b'\nET'
                full_new = b'BT\n' + new_block + b'\nET'
                
                replacements.append((match.start(), match.end(), full_new, field_name, old_text, new_value))
                logger.info(f"[OK] {field_name}: '{old_text[:15]}' -> '{new_value[:15]}'")
                found = True
                break
        
        if not found:
            logger.warning(f"[!] {field_name}: не найден (Y={target_y})")
    
    if not replacements:
        logger.info("No changes")
        return bytes(pdf_bytes)
    
    # Применяем замены
    # Нужно заменять в decompressed stream
    # Используем позиции match.start() и match.end() которые относятся к decompressed
    replacements.sort(key=lambda r: r[0], reverse=True)
    
    for start, end, new_bytes, field_name, old_text, new_value in replacements:
        # Оригинальный BT...ET блок
        old_block = decompressed[start:end]
        # Заменяем
        decompressed = decompressed[:start] + bytearray(new_bytes) + decompressed[end:]
    
    logger.info(f"Applied {len(replacements)} changes")
    
    # Сжимаем
    new_compressed = zlib.compress(bytes(decompressed), 9)
    logger.info(f"Compression: {original_compressed_len} -> {len(new_compressed)}")
    
    # Обновляем /Length
    length_pattern = rb'/Length\s+(\d+)'
    length_match = re.search(length_pattern, pdf_bytes[obj_start:stream_keyword])
    if length_match:
        old_len_str = length_match.group(1)
        new_len_str = str(len(new_compressed)).encode()
        
        if len(new_len_str) < len(old_len_str):
            new_len_str = new_len_str + b' ' * (len(old_len_str) - len(new_len_str))
        
        len_pos = obj_start + length_match.start(1)
        pdf_bytes[len_pos:len_pos + len(old_len_str)] = new_len_str[:len(old_len_str)]
    
    # Заменяем stream
    pdf_bytes = pdf_bytes[:stream_start] + bytearray(new_compressed) + pdf_bytes[stream_end:]
    
    # Пересчитываем xref
    result = rebuild_xref(bytes(pdf_bytes))
    
    logger.info(f"RESULT: {original_size} -> {len(result)} ({len(result)-original_size:+d})")
    return result


if __name__ == "__main__":
    data = {
        'date_time': "26.01.2026 15:03",
        'amount': "50 000 ₽",
        'sender_name': "Данил Сергеевич Д.",
        'receiver_name': "Иван Иванович И.",
        'receiver_phone': "+7 (999) 123-45-67",
        'recipient_bank': "СБП",
    }
    
    result = create_ozon_stealth(data=data, auto_select=True)
    
    if result:
        with open("test_ozon_v3.pdf", "wb") as f:
            f.write(result)
        logger.info(f"[OK] Saved: test_ozon_v3.pdf ({len(result)} bytes)")
    else:
        logger.error("[X] Failed to create PDF")
