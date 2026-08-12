"""
Менеджер шаблонов Sberbank - автоматически выбирает шаблон с нужными буквами.
"""

import os
import json
import fitz
import zlib
import re
import logging
from typing import Dict, Set, Optional, List

logger = logging.getLogger(__name__)

TEMPLATES_DIR = "templates"
CACHE_FILE = "templates/sber_templates_cache.json"


def extract_available_chars_from_pdf(pdf_path: str) -> Set[str]:
    """Извлекает доступные символы из шрифта PDF"""
    
    available = set()
    
    try:
        doc = fitz.open(pdf_path)
        
        # Ищем ToUnicode CMap
        for xref in range(1, doc.xref_length()):
            try:
                stream = doc.xref_stream_raw(xref)
                if not stream:
                    continue
                    
                # Декомпрессируем
                try:
                    stream = zlib.decompress(stream)
                except:
                    pass
                
                if b'beginbfrange' not in stream:
                    continue
                
                # Парсим bfrange
                content = stream.decode('latin-1', errors='ignore')
                
                # Ищем строки типа <CID> <CID> <UNICODE>
                pattern = r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>'
                for match in re.finditer(pattern, content):
                    cid_start = int(match.group(1), 16)
                    cid_end = int(match.group(2), 16)
                    unicode_start = int(match.group(3), 16)
                    
                    for i in range(cid_end - cid_start + 1):
                        try:
                            char = chr(unicode_start + i)
                            available.add(char)
                        except:
                            pass
                
                break  # Нашли ToUnicode, выходим
                
            except:
                pass
                
        doc.close()
        
    except Exception as e:
        logger.error(f"Ошибка при анализе {pdf_path}: {e}")
    
    return available


def scan_templates() -> Dict[str, Set[str]]:
    """Сканирует все PDF в папке templates и возвращает доступные символы для каждого"""
    
    templates = {}
    
    if not os.path.exists(TEMPLATES_DIR):
        os.makedirs(TEMPLATES_DIR)
        return templates
    
    for filename in os.listdir(TEMPLATES_DIR):
        if filename.endswith('.pdf') and not filename.startswith('test'):
            pdf_path = os.path.join(TEMPLATES_DIR, filename)
            
            logger.info(f"Сканирую: {filename}")
            available = extract_available_chars_from_pdf(pdf_path)
            
            if available:
                templates[pdf_path] = available
                logger.info(f"  Найдено {len(available)} символов")
    
    return templates


def save_cache(templates: Dict[str, Set[str]]):
    """Сохраняет кэш шаблонов"""
    cache = {path: list(chars) for path, chars in templates.items()}
    
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    
    logger.info(f"Кэш сохранён: {CACHE_FILE}")


def load_cache() -> Optional[Dict[str, Set[str]]]:
    """Загружает кэш шаблонов"""
    if not os.path.exists(CACHE_FILE):
        return None
    
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        
        return {path: set(chars) for path, chars in cache.items()}
    except:
        return None


def get_templates_info(force_rescan: bool = False) -> Dict[str, Set[str]]:
    """Получает информацию о шаблонах (из кэша или сканирует)"""
    
    if not force_rescan:
        cached = load_cache()
        if cached:
            # Проверяем что файлы существуют
            valid = {p: c for p, c in cached.items() if os.path.exists(p)}
            if valid:
                return valid
    
    # Сканируем заново
    logger.info("Сканирование шаблонов...")
    templates = scan_templates()
    
    if templates:
        save_cache(templates)
    
    return templates


def normalize_text(text: str) -> str:
    """Нормализует текст - заменяет эквивалентные символы"""
    # Обычный дефис '-' оставляем как есть
    return text


def find_template_for_text(text: str, templates: Dict[str, Set[str]] = None) -> Optional[str]:
    """
    Находит шаблон который содержит все символы из текста.
    
    Args:
        text: Текст для проверки (все поля объединённые)
        templates: Словарь шаблонов (если None - загрузит автоматически)
    
    Returns:
        Путь к подходящему шаблону или None
    """
    
    if templates is None:
        templates = get_templates_info()
    
    if not templates:
        logger.error("Нет доступных шаблонов!")
        return None
    
    # Нормализуем текст
    text = normalize_text(text)
    
    # Символы которые нужны
    needed = set(text)
    needed.discard(' ')
    needed.discard('\n')
    needed.discard('\t')
    
    # Ищем подходящий шаблон
    best_template = None
    best_extra = float('inf')  # Предпочитаем шаблон с минимумом лишних символов
    
    for template_path, available in templates.items():
        missing = needed - available
        
        if not missing:
            # Все символы есть!
            extra = len(available - needed)
            if extra < best_extra:
                best_template = template_path
                best_extra = extra
    
    return best_template


def find_template_for_data(data: Dict[str, str]) -> Optional[str]:
    """
    Находит шаблон для данных чека.
    
    Args:
        data: Словарь с данными чека
    
    Returns:
        Путь к подходящему шаблону или None
    """
    
    # Объединяем все текстовые значения
    all_text = ''.join(str(v) for v in data.values())
    
    return find_template_for_text(all_text)


def get_missing_chars_for_data(data: Dict[str, str], templates: Dict[str, Set[str]] = None) -> List[str]:
    """
    Возвращает список недостающих символов (которых нет ни в одном шаблоне).
    """
    
    if templates is None:
        templates = get_templates_info()
    
    if not templates:
        return list(set(''.join(str(v) for v in data.values())))
    
    # Объединяем все доступные символы из всех шаблонов
    all_available = set()
    for available in templates.values():
        all_available.update(available)
    
    # Нормализуем текст данных
    all_text = normalize_text(''.join(str(v) for v in data.values()))
    
    # Символы которые нужны
    needed = set(all_text)
    needed.discard(' ')
    needed.discard('\n')
    needed.discard('\t')
    
    # Недостающие
    missing = needed - all_available
    
    return sorted(list(missing))


# ============================================================
# CLI для тестирования
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    print("="*60)
    print("МЕНЕДЖЕР ШАБЛОНОВ SBERBANK")
    print("="*60)
    
    # Сканируем шаблоны
    print("\n1. Сканирование шаблонов...")
    templates = get_templates_info(force_rescan=True)
    
    print(f"\nНайдено шаблонов: {len(templates)}")
    
    for path, chars in templates.items():
        cyrillic = [c for c in chars if '\u0400' <= c <= '\u04FF']
        print(f"\n{os.path.basename(path)}:")
        print(f"  Всего символов: {len(chars)}")
        print(f"  Кириллица ({len(cyrillic)}): {''.join(sorted(cyrillic))}")
    
    # Тестируем поиск
    print("\n" + "="*60)
    print("2. Тест поиска шаблона")
    print("="*60)
    
    test_data = {
        'sender': "Иван Петров",
        'receiver': "Алексей Сидоров",
        'amount': "5 000 ₽",
    }
    
    template = find_template_for_data(test_data)
    if template:
        print(f"\n[OK] Найден шаблон: {template}")
    else:
        missing = get_missing_chars_for_data(test_data, templates)
        print(f"\n[X] Подходящий шаблон не найден!")
        print(f"   Недостающие символы: {missing}")
