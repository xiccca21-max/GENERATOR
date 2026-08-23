# -*- coding: utf-8 -*-
"""QR login for the @bankpdfbot account (separate from KRÖN / OnlyPDF).

Saves PNG + HTML every ~25s. Open output/bankpdf_qr.html or http://127.0.0.1:8765/
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
sys.path.insert(0, str(_DIR))

from tg_check_pdf import _load_env  # noqa: E402
from tg_client import connect_client, make_client  # noqa: E402

_PNG = _ROOT / "output" / "bankpdf_qr.png"
_HTML = _ROOT / "output" / "bankpdf_qr.html"
_SESSION = _DIR / ".tg_bankpdf_session"


def _save_qr(url: str) -> None:
    import qrcode

    _PNG.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(_PNG)
    b64 = base64.b64encode(_PNG.read_bytes()).decode("ascii")
    _HTML.write_text(
        f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="refresh" content="2" />
  <title>QR @bankpdfbot</title>
  <style>
    html, body {{ margin: 0; background: #111; color: #eee; font-family: sans-serif; text-align: center; }}
    img {{ width: min(80vw, 80vh); height: auto; background: #fff; padding: 20px; margin-top: 20px; }}
    p {{ font-size: 20px; }}
  </style>
</head>
<body>
  <p>Аккаунт для @bankpdfbot — Telegram → Настройки → Устройства → Подключить устройство</p>
  <img alt="Telegram QR" src="data:image/png;base64,{b64}" />
  <p>Страница обновляется сама. Если «неверный» — подожди секунду.</p>
</body>
</html>
""",
        encoding="utf-8",
    )
    print(f"QR refreshed → {_HTML}", flush=True)


async def main() -> None:
    cfg = _load_env()
    cfg["TG_SESSION"] = str(_SESSION)
    print("=== @bankpdfbot account QR login ===", flush=True)
    print("Telegram → Настройки → Устройства → Подключить устройство", flush=True)

    client = make_client(cfg)
    await connect_client(client, cfg)
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already OK: {me.first_name} (@{me.username or '-'}) id={me.id}", flush=True)
        await client.disconnect()
        return

    from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
    from telethon.tl.functions.account import GetPasswordRequest

    pwd = (os.environ.get("TG_2FA") or "").strip()
    if pwd:
        try:
            info = await client(GetPasswordRequest())
            hint = (info.hint or "").strip()
            if hint:
                print(f"HINT: {hint}", flush=True)
            await client.sign_in(password=pwd)
        except PasswordHashInvalidError:
            print("WRONG_2FA", flush=True)
            try:
                hint = ((await client(GetPasswordRequest())).hint or "").strip()
            except Exception:
                hint = ""
            if hint:
                print(f"HINT: {hint}", flush=True)
            else:
                print("HINT: (пустая — Telegram не отдал подсказку)", flush=True)
            await client.disconnect()
            return
        except Exception as exc:
            print(f"2FA_PRE {type(exc).__name__}: {exc}", flush=True)

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"OK: {me.first_name} (@{me.username or '-'}) id={me.id}", flush=True)
        await client.disconnect()
        return

    qr_login = await client.qr_login()
    _save_qr(qr_login.url)

    deadline = asyncio.get_event_loop().time() + 600
    refresh_every = 25.0
    last_refresh = asyncio.get_event_loop().time()

    while asyncio.get_event_loop().time() < deadline:
        try:
            await asyncio.wait_for(qr_login.wait(timeout=refresh_every), timeout=refresh_every + 2)
            break
        except (asyncio.TimeoutError, TimeoutError):
            now = asyncio.get_event_loop().time()
            if now - last_refresh >= refresh_every - 1:
                try:
                    await qr_login.recreate()
                    _save_qr(qr_login.url)
                    last_refresh = now
                except Exception:
                    qr_login = await client.qr_login()
                    _save_qr(qr_login.url)
                    last_refresh = now
            continue
        except SessionPasswordNeededError:
            print("NEED_2FA — облачный пароль Telegram", flush=True)
            from telethon.errors import PasswordHashInvalidError
            from telethon.tl.functions.account import GetPasswordRequest

            hint = ""
            try:
                hint = (await client(GetPasswordRequest())).hint or ""
            except Exception:
                hint = ""
            if hint:
                print(f"HINT: {hint}", flush=True)
            pwd = (os.environ.get("TG_2FA") or "").strip()
            if not pwd:
                print("Задай TG_2FA и перезапусти, либо напиши пароль в чат", flush=True)
                await client.disconnect()
                return
            try:
                await client.sign_in(password=pwd)
            except PasswordHashInvalidError:
                print("WRONG_2FA", flush=True)
                if hint:
                    print(f"HINT: {hint}", flush=True)
                await client.disconnect()
                return
            break

    if not await client.is_user_authorized():
        print("NOT_AUTHORIZED — время вышло, запусти снова", flush=True)
        await client.disconnect()
        return

    me = await client.get_me()
    print(f"OK: {me.first_name} (@{me.username or '-'}) id={me.id}", flush=True)
    print(f"Session: {_SESSION}.session", flush=True)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
