"""Fill the Alfa userscript template with parsed payload + PDF base64."""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path
from typing import Any

import fitz

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.alfa_mock.parse import parse_payload  # noqa: E402
ALFA_TEMPLATE = Path(__file__).with_name("template.user.js")
TBANK_TEMPLATE = Path(__file__).resolve().parents[1] / "pdf_forge" / "template_tbank.user.js"
PLACEHOLDER = "__PDF_FORGE_CONFIG__"
USERSCRIPT_VERSION = "1.3.58"
_OZON_PNG = Path(__file__).with_name("logo_bank_ozon_ecom.png")
_TBANK_PNG = Path(__file__).with_name("logo_bank_tinkoff_v2.png")
_ALFA_PNG = Path(__file__).with_name("logo_bank_alfabank.png")
_BSPB_PNG = Path(__file__).with_name("logo_bank_bspb.png")


def _png_data_uri(path: Path) -> str:
    if not path.is_file():
        return ""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _ozon_logo_data_uri() -> str:
    return _png_data_uri(_OZON_PNG)


def _tbank_logo_data_uri() -> str:
    return _png_data_uri(_TBANK_PNG)


def _alfa_logo_data_uri() -> str:
    return _png_data_uri(_ALFA_PNG)


def _bspb_logo_data_uri() -> str:
    return _png_data_uri(_BSPB_PNG)


def _pack_op_bank_logos(ops: list) -> None:
    """CDN logos 5xx often; stamp packed data: URIs onto op.bank."""
    packed_by_key = {
        "Ozon": _ozon_logo_data_uri(),
        "Т-Банк": _tbank_logo_data_uri(),
        "Альфа": _alfa_logo_data_uri(),
        "БСПБ": _bspb_logo_data_uri(),
    }
    for op in ops:
        bank = op.get("bank") if isinstance(op, dict) else None
        if not isinstance(bank, dict):
            continue
        key = str(bank.get("key") or "")
        name = str(bank.get("name") or "").lower()
        packed = packed_by_key.get(key) or ""
        if not packed:
            if "т-банк" in name or "tinkoff" in name:
                packed = packed_by_key["Т-Банк"]
            elif "альфа" in name or "alfa" in name:
                packed = packed_by_key["Альфа"]
            elif "ozon" in name or "озон" in name:
                packed = packed_by_key["Ozon"]
            elif "бспб" in name or "bspb" in name or "санкт" in name:
                packed = packed_by_key["БСПБ"]
        if packed:
            bank["logoUrl"] = packed
            bank["iconUrl"] = packed
            bank["logo"] = packed


def filename_for(data: dict[str, Any], cabinet: str = "alfa") -> str:
    parts = [data.get("lastName") or "", data.get("firstName") or "", data.get("patronymic") or ""]
    slug = "_".join(p for p in parts if p)
    slug = re.sub(r"[^\w]+", "_", slug, flags=re.UNICODE).strip("_")
    prefix = "tbank_mock" if cabinet == "tbank" else "alfa_mock"
    return f"{prefix}_{slug or 'user'}_user.js"


def build_config(
    data: dict[str, Any],
    pdfs: list[bytes],
    *,
    split_check: bool = False,
) -> dict[str, Any]:
    def _pdf_slot(raw: bytes) -> dict[str, str]:
        slot = {"pdfBase64": "", "previewBase64": ""}
        if not raw:
            return slot
        slot["pdfBase64"] = base64.b64encode(raw).decode("ascii")
        try:
            with fitz.open(stream=raw, filetype="pdf") as doc:
                page = doc.load_page(0)
                pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                slot["previewBase64"] = base64.b64encode(pix.tobytes("png")).decode("ascii")
        except Exception:
            slot["previewBase64"] = ""
        return slot

    n_ops = min(len(data.get("operations") or []), 3)
    # Receipts = first n_ops PDFs only. Statement is pdfs[n_ops] and MUST
    # not also land in receipts[n_ops] (that made «Получить квитанцию» open the statement).
    receipts = [
        _pdf_slot(pdfs[i] if i < n_ops and i < len(pdfs) else b"")
        for i in range(3)
    ]
    statement = _pdf_slot(pdfs[n_ops] if len(pdfs) > n_ops else b"")
    ops = list(data.get("operations") or [])
    bank_logos: dict[str, str] = {}
    ozon = _ozon_logo_data_uri()
    if ozon:
        bank_logos["Ozon"] = ozon
    tbank = _tbank_logo_data_uri()
    if tbank:
        bank_logos["Т-Банк"] = tbank
    alfa = _alfa_logo_data_uri()
    if alfa:
        bank_logos["Альфа"] = alfa
    bspb = _bspb_logo_data_uri()
    if bspb:
        bank_logos["БСПБ"] = bspb
    _pack_op_bank_logos(ops)
    return {
        "enabled": True,
        "splitCheck": bool(split_check),
        "splitCheckLink": "https://money-alfabank.ru/mr/wK4nRmQp8d",
        "profile": {
            "firstName": data["firstName"],
            "lastName": data["lastName"],
            "patronymic": data.get("patronymic") or "",
            "displayName": data["displayName"],
            "email": data["email"],
            "mobilePhoneNumber": data["mobilePhoneNumber"],
        },
        "balance": {"value": float(data["balance"])},
        "spending": {
            "monthTotal": float(data["spending"]),
            "incomeTotal": float(data["income"]),
        },
        "operations": ops,
        "operationsRecentCount": len(ops),
        "account": {
            "last4": str(data.get("accountLast4") or ""),
            "number": str(data.get("accountNumber") or ""),
        },
        "statement": statement,
        "bankLogos": bank_logos,
        "receipts": receipts,
    }


def assemble(
    data: dict[str, Any],
    pdfs: list[bytes] | None = None,
    cabinet: str = "alfa",
    *,
    split_check: bool = False,
) -> str:
    path = TBANK_TEMPLATE if cabinet == "tbank" else ALFA_TEMPLATE
    tpl = path.read_text(encoding="utf-8")
    if PLACEHOLDER not in tpl:
        raise RuntimeError(f"template missing {PLACEHOLDER}: {path}")
    dumped = json.dumps(
        build_config(data, pdfs or [], split_check=bool(split_check) and cabinet == "alfa"),
        ensure_ascii=False,
        indent=2,
    )
    return tpl.replace(PLACEHOLDER, dumped)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Assemble Alfa mock userscript")
    p.add_argument("--text", required=True, help="payload .txt (тот же формат, что в боте)")
    p.add_argument("--pdf", action="append", default=[], help="PDF in order (first = newest op)")
    p.add_argument("--cabinet", choices=("alfa", "tbank"), default="alfa")
    p.add_argument(
        "--split-check",
        action="store_true",
        help="Alfa: кнопка «Разделить чек» + фиксированная ссылка",
    )
    p.add_argument("--out", help="output .user.js")
    args = p.parse_args(argv)
    payload = Path(args.text).read_text(encoding="utf-8")
    data = parse_payload(payload, args.cabinet)
    pdfs = [Path(x).read_bytes() for x in args.pdf]
    js = assemble(data, pdfs, args.cabinet, split_check=bool(args.split_check))
    out = Path(args.out) if args.out else Path(filename_for(data, args.cabinet))
    out.write_text(js, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
