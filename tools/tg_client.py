"""Общее подключение Telethon (таймаут, прокси, вход)."""
from __future__ import annotations

import sys
import winreg
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

_DIR = Path(__file__).resolve().parent
_SESSION = str(_DIR / ".tg_checker_session")


def session_path(cfg: Optional[Dict[str, str]] = None) -> str:
    """Session file stem (Telethon adds .session).

    OnlyPDF / default: tools/.tg_checker_session
    Fraudex parallel: TG_SESSION=.../.tg_fraudex_session (separate SQLite lock).
    """
    raw = ""
    if cfg:
        raw = (cfg.get("TG_SESSION") or "").strip()
    if not raw:
        return _SESSION
    p = Path(raw)
    if not p.is_absolute():
        p = _DIR / p
    # Telethon expects path without .session suffix
    if p.suffix == ".session":
        p = p.with_suffix("")
    return str(p)


def _parse_proxy(raw: str, default_kind: str = "socks5") -> Optional[Tuple]:
    raw = (raw or "").strip()
    if not raw:
        return None

    kind = (default_kind or "socks5").lower()
    if kind == "https":
        kind = "http"

    # ip:port:user:pass
    if not raw.startswith(("socks5://", "socks4://", "http://", "https://")):
        parts = raw.split(":")
        if len(parts) >= 4:
            host, port_s, user = parts[0], parts[1], parts[2]
            pwd = ":".join(parts[3:])
            return (kind, host, int(port_s), True, user, pwd)
        if len(parts) == 2:
            return (kind, parts[0], int(parts[1]))

    u = urlparse(raw)
    scheme = (u.scheme or kind).lower()
    if scheme in ("http", "https"):
        pkind = "http"
    elif scheme == "socks4":
        pkind = "socks4"
    else:
        pkind = "socks5"
    host = u.hostname or "127.0.0.1"
    port = int(u.port or 1080)
    if u.username:
        return (pkind, host, port, True, u.username, u.password or "")
    return (pkind, host, port)


def _windows_system_proxy() -> Optional[Tuple]:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            proxy, _ = winreg.QueryValueEx(key, "ProxyServer")
            if not proxy:
                return None
            part = proxy.split(";")[0]
            if "=" in part:
                part = part.split("=", 1)[1]
            host, port_s = part.split(":")
            return ("http", host, int(port_s))
    except OSError:
        return None


def resolve_proxy(cfg: Dict[str, str]) -> Optional[Tuple]:
    raw = cfg.get("TG_PROXY", "").strip()
    if raw:
        return _parse_proxy(raw, cfg.get("TG_PROXY_TYPE", "socks5"))
    return _windows_system_proxy()


def _proxy_label(proxy: Optional[Tuple]) -> str:
    if not proxy:
        return "нет"
    if len(proxy) >= 6:
        return f"{proxy[0]}://{proxy[1]}:{proxy[2]} (auth)"
    return f"{proxy[0]}://{proxy[1]}:{proxy[2]}"


def make_client(cfg: Dict[str, str]):
    from telethon import TelegramClient

    api_id = int(cfg["TG_API_ID"])
    api_hash = cfg["TG_API_HASH"]
    proxy = resolve_proxy(cfg)
    sess = session_path(cfg)
    print(f"TG session: {sess}", flush=True)
    return TelegramClient(
        sess,
        api_id,
        api_hash,
        proxy=proxy,
        connection_retries=2,
        retry_delay=1,
        timeout=20,
        request_retries=2,
    )


async def connect_client(client, cfg: Optional[Dict[str, str]] = None) -> None:
    import asyncio
    import sqlite3

    if cfg is None:
        from tg_check_pdf import _load_env
        cfg = _load_env()
    print(f"Прокси: {_proxy_label(resolve_proxy(cfg))}", flush=True)
    print("Подключаюсь к Telegram...", flush=True)
    last_err: Optional[Exception] = None
    for attempt in range(8):
        try:
            await client.connect()
            return
        except sqlite3.OperationalError as e:
            last_err = e
            await asyncio.sleep(1.0 + attempt * 0.5)
        except OSError as e:
            last_err = e
            if attempt >= 1:
                break
            await asyncio.sleep(2.0)
    print("\nНе могу подключиться к Telegram.")
    print("Проверь TG_PROXY / TG_PROXY_TYPE в tools/tg_check.env")
    if last_err:
        print(f"Ошибка: {last_err}")
    sys.exit(1)


async def login_with_qr(
    client,
    timeout: int = 180,
    cfg: Optional[Dict[str, str]] = None,
) -> None:
    """Вход по QR — без SMS. Если включена 2FA — спросит пароль после скана."""
    from telethon.errors import SessionPasswordNeededError

    print("\n=== ВХОД ПО QR ===")
    print("1. Telegram на телефоне")
    print("2. Настройки -> Устройства -> Подключить устройство")
    print("3. Сканируй QR в этом окне\n")

    qr_login = await client.qr_login()
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_login.url)
        qr.make()
        qr.print_ascii(invert=True)
    except ImportError:
        print("(pip install qrcode — покажет QR в консоли)")

    print(f"\nИли открой ссылку на телефоне:\n{qr_login.url}\n")
    print(f"Жду сканирование ({timeout} сек)...", flush=True)

    need_2fa = False
    try:
        await qr_login.wait(timeout)
    except SessionPasswordNeededError:
        need_2fa = True
    except Exception as e:
        err = str(e).lower()
        if "two-steps" in err or "password is required" in err or "sessionpassword" in err:
            need_2fa = True
        else:
            print(f"\nQR не подтвердили: {e}")
            print("Запусти снова: python tools/tg_login_fraudex.py")
            sys.exit(1)

    if need_2fa or not await client.is_user_authorized():
        print("\nНа аккаунте включена 2FA — нужен облачный пароль Telegram.")
        pwd = input("Пароль 2FA: ").strip()
        try:
            await client.sign_in(password=pwd)
        except SessionPasswordNeededError:
            print("Неверный пароль 2FA.")
            sys.exit(1)

    me = await client.get_me()
    print(f"\nOK: {me.first_name} (@{me.username or '-'})")
    print(f"Сессия: {session_path(cfg)}.session\n")


async def ensure_login(client, cfg: Optional[Dict[str, str]] = None) -> None:
    """Для проверки PDF: нужна готовая сессия."""
    import asyncio
    import sqlite3

    await connect_client(client, cfg)
    if not await client.is_user_authorized():
        print("\nСессии нет. Сначала войди по QR:")
        print("  python tools/tg_login_once.py")
        sys.exit(1)
    for attempt in range(8):
        try:
            me = await client.get_me()
            print(f"OK: {me.first_name} (@{me.username or '-'})\n")
            return
        except sqlite3.OperationalError:
            await asyncio.sleep(1.0 + attempt * 0.5)
    print("\nСессия Telethon занята другим процессом. Останови другие fraudex_streak30.")
    sys.exit(1)
