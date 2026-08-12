"""
Конфигурация Telegram бота - UMBRA SYNDICATE

ИНСТРУКЦИЯ:
1. Получите токен у @BotFather в Telegram
2. Положите BOT_TOKEN в .env (или EnvironmentFile в systemd)
3. Настройте цены и пакеты
4. Запустите бота: python bot.py
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# Токен бота от @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Админы - ТОЛЬКО эти username (без @). ID подтягиваются автоматически.
ADMIN_USERNAMES = {"acterichee", "kronlead"}
# Опционально явные ID (обычно пусто - резолв по username)
ADMIN_IDS: list[int] = []

# ========== НАСТРОЙКИ ВАЛЮТЫ ==========

# Название валюты (используется в интерфейсе)
CURRENCY_NAME = "💎"  # Можно: ⭐ 💎 🔮 ⚡ 🪙

# Курс: сколько монет за 1 USDT
COINS_PER_USDT = 1  # 1 USDT = 1 монета

# Минимальное пополнение (в USDT)
MIN_DEPOSIT = 3

# ========== ЦЕНЫ НА ИНСТРУМЕНТЫ (в монетах за 1 использование) ==========

TOOL_PRICES = {
    'vtb_check': 2,
    'sber_check': 2,
    'ozon_check': 2,
    'alfa_check': 2,
    'tbank_check': 2,
    'otp_check': 2,
    'spoof_email': 10,
    'tg_accounts': 15,
    'voice_clone': 20,
    'chat_draw': 8,
    'doc_draw': 10,
    'ai_uncensored': 3,
}

# ========== АКЦИОННЫЕ ПАКЕТЫ (legacy unlimited) ==========
PACKAGES = {
    'checks_unlimited': {
        'name': '💳 Чеки безлимит',
        'description': 'Безлимит на все банковские чеки',
        'unlimited_tools': ['vtb_check', 'sber_check', 'ozon_check', 'alfa_check', 'tbank_check', 'otp_check'],
        'duration_days': 30,
        'price_usdt': 50,
    },
    'full_access': {
        'name': '👑 Полный доступ',
        'description': 'Безлимит на ВСЕ инструменты',
        'unlimited_tools': ['all'],
        'duration_days': 30,
        'price_usdt': 150,
    },
    'starter': {
        'name': '🚀 Стартер',
        'description': 'Безлимит чеки + 50 монет',
        'unlimited_tools': ['vtb_check', 'sber_check', 'ozon_check', 'alfa_check', 'tbank_check', 'otp_check'],
        'bonus_coins': 50,
        'duration_days': 30,
        'price_usdt': 70,
    },
}

# Пакеты чеков (1 чек = 2$; оптом дешевле)
# 1×2=2 | 10×2=20 → 15 | 50×2=100 → 60
CHECK_PACKAGES = {
    "pack1": {"name": "1 чек", "checks": 1, "price_usdt": 2, "btn": "📦 1 чек - 2$"},
    "pack10": {"name": "10 чеков", "checks": 10, "price_usdt": 15, "btn": "📦 10 чеков - 15$"},
    "pack50": {"name": "50 чеков", "checks": 50, "price_usdt": 60, "btn": "📦 50 чеков - 60$"},
}

# Пресеты пополнения баланса (USDT → монеты 1:1 при COINS_PER_USDT=1)
TOPUP_PRESETS_USDT = (5, 10, 25, 50)

# Алерт админам при пополнении от этой суммы (USDT)
LARGE_DEPOSIT_USDT = int(os.getenv("LARGE_DEPOSIT_USDT", "50"))

# ========== НАСТРОЙКИ ОПЛАТЫ ==========

# Crypto Pay API token от @CryptoBot → Crypto Pay → Create App
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "").strip()

# USDT адрес для приёма платежей (TRC20) - legacy
USDT_ADDRESS = os.getenv("USDT_ADDRESS", "")

# Файл с данными пользователей
PAYMENTS_FILE = "payments.json"

# ========== ТЕСТОВЫЙ РЕЖИМ ==========

# Включить тестовый режим (бесплатные использования)
TRIAL_ENABLED = False

# Сколько бесплатных использований каждого инструмента
TRIAL_USES = {
    'vtb_check': 2,
    'sber_check': 2,
    'ozon_check': 2,
    'alfa_check': 2,
    'tbank_check': 2,
    'otp_check': 2,
    'spoof_email': 1,
    'tg_accounts': 1,
    'voice_clone': 1,
    'chat_draw': 1,
    'doc_draw': 1,
    'ai_uncensored': 5,
}
