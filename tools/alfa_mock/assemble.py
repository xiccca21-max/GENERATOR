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
USERSCRIPT_VERSION = "1.3.36"


def filename_for(data: dict[str, Any], cabinet: str = "alfa") -> str:
    parts = [data.get("lastName") or "", data.get("firstName") or "", data.get("patronymic") or ""]
    slug = "_".join(p for p in parts if p)
    slug = re.sub(r"[^\w]+", "_", slug, flags=re.UNICODE).strip("_")
    prefix = "tbank_mock" if cabinet == "tbank" else "alfa_mock"
    return f"{prefix}_{slug or 'user'}_user.js"


def build_config(data: dict[str, Any], pdfs: list[bytes]) -> dict[str, Any]:
    receipts = [{"pdfBase64": "", "previewBase64": ""} for _ in range(3)]
    for i, raw in enumerate(pdfs[:3]):
        if raw:
            receipts[i]["pdfBase64"] = base64.b64encode(raw).decode("ascii")
            try:
                with fitz.open(stream=raw, filetype="pdf") as doc:
                    page = doc.load_page(0)
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                    receipts[i]["previewBase64"] = base64.b64encode(pix.tobytes("png")).decode("ascii")
            except Exception:
                receipts[i]["previewBase64"] = ""
    ops = list(data.get("operations") or [])
    return {
        "enabled": True,
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
        "receipts": receipts,
    }


def assemble(data: dict[str, Any], pdfs: list[bytes] | None = None, cabinet: str = "alfa") -> str:
    path = TBANK_TEMPLATE if cabinet == "tbank" else ALFA_TEMPLATE
    tpl = path.read_text(encoding="utf-8")
    if PLACEHOLDER not in tpl:
        raise RuntimeError(f"template missing {PLACEHOLDER}: {path}")
    dumped = json.dumps(build_config(data, pdfs or []), ensure_ascii=False, indent=2)
    return tpl.replace(PLACEHOLDER, dumped)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Assemble Alfa mock userscript")
    p.add_argument("--text", required=True, help="payload .txt (тот же формат, что в боте)")
    p.add_argument("--pdf", action="append", default=[], help="PDF in order (first = newest op)")
    p.add_argument("--cabinet", choices=("alfa", "tbank"), default="alfa")
    p.add_argument("--out", help="output .user.js")
    args = p.parse_args(argv)
    payload = Path(args.text).read_text(encoding="utf-8")
    data = parse_payload(payload, args.cabinet)
    pdfs = [Path(x).read_bytes() for x in args.pdf]
    js = assemble(data, pdfs, args.cabinet)
    out = Path(args.out) if args.out else Path(filename_for(data, args.cabinet))
    out.write_text(js, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
