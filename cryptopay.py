"""Crypto Pay API (@CryptoBot) — create/check invoices."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

API_BASE = (os.getenv("CRYPTO_PAY_API") or "https://pay.crypt.bot/api").rstrip("/")


class CryptoPayError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _token() -> str:
    return (os.getenv("CRYPTO_PAY_TOKEN") or "").strip()


def is_configured() -> bool:
    return bool(_token())


def api_call(method: str, payload: dict | None = None) -> Any:
    token = _token()
    if not token:
        raise CryptoPayError("CRYPTO_PAY_TOKEN не задан")
    url = f"{API_BASE}/{method}"
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Crypto-Pay-API-Token": token,
            "Content-Type": "application/json",
            "User-Agent": "ReceiptBot/1.0 (+https://t.me/CryptoBot)",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8")
        except Exception:
            raise CryptoPayError(f"HTTP {e.code}") from e
    except Exception as e:
        raise CryptoPayError(str(e)) from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CryptoPayError("bad JSON from CryptoPay") from e

    if not data.get("ok"):
        err = data.get("error") or {}
        raise CryptoPayError(
            err.get("name") or err.get("message") or str(err) or "CryptoPay error",
            code=err.get("code"),
        )
    return data.get("result")


def create_invoice(
    *,
    amount_usdt: float | int | str,
    description: str,
    payload: str,
    expires_in: int = 1800,
) -> dict:
    """Create USDT invoice. Returns invoice dict with invoice_id, bot_invoice_url/pay_url."""
    amount = f"{float(amount_usdt):.2f}"
    result = api_call(
        "createInvoice",
        {
            "asset": "USDT",
            "amount": amount,
            "description": description[:1024],
            "payload": payload[:4096],
            "expires_in": int(expires_in),
            "allow_comments": False,
            "allow_anonymous": False,
        },
    )
    if not isinstance(result, dict):
        raise CryptoPayError("empty invoice")
    return result


def get_invoice(invoice_id: int | str) -> dict | None:
    result = api_call("getInvoices", {"invoice_ids": str(invoice_id)})
    if isinstance(result, dict):
        items = result.get("items") or []
    elif isinstance(result, list):
        items = result
    else:
        items = []
    return items[0] if items else None


def invoice_pay_url(invoice: dict) -> str:
    return (
        invoice.get("bot_invoice_url")
        or invoice.get("pay_url")
        or invoice.get("mini_app_invoice_url")
        or ""
    )
