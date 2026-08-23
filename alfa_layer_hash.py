# -*- coding: utf-8 -*-
"""Layered hash-report for Alfa Oracle PDFs.

Competitor does not need a full-file MD5 match. Compare layers:
images/shell must equal the donor; Content, FontFile2 and /ID must be new.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

import fitz

_ID_RE = re.compile(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]")
_HEX_TJ_RE = re.compile(rb"<[0-9A-Fa-f]+>")
_DATE_RE = re.compile(rb"\(D:[0-9]{14}(?:[+\-][0-9]{2}'[0-9]{2}')?\)")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def _md5(data: bytes) -> str:
    return hashlib.md5(data or b"").hexdigest()


def _sha16(data: bytes) -> str:
    return _sha256(data)[:16]


def _role(obj: str, decoded: bytes) -> str:
    if "/Subtype /Image" in obj:
        return "image"
    if "/FontFile2" in obj or decoded[:4] == b"\x00\x01\x00\x00":
        return "font"
    if b"beginbfchar" in decoded or b"beginbfrange" in decoded:
        return "tounicode"
    if b"BT" in decoded and (b"Tj" in decoded or b"TJ" in decoded):
        return "content"
    if "/Type /Metadata" in obj or decoded[:5] == b"<?xmp":
        return "metadata"
    return "other"


def _xobject_map(doc: fitz.Document) -> Dict[str, int]:
    """Page /XObject names → xref (Im0/Im1/Im2)."""
    out: Dict[str, int] = {}
    try:
        raw = doc.xref_object(doc[0].xref) or ""
    except Exception:
        return out
    rm = re.search(r"/Resources\s+(\d+)\s+0\s+R", raw)
    res = doc.xref_object(int(rm.group(1))) if rm else raw
    xm = re.search(r"/XObject\s*<<((?:[^>]|>(?!>))*)>>", res or "", re.S)
    if not xm:
        return out
    for name, xref in re.findall(r"/(Im[0-9]+)\s+(\d+)\s+0\s+R", xm.group(1)):
        out[name] = int(xref)
    return out


def layer_report(pdf: bytes) -> Dict[str, Any]:
    """Hash each stable/dynamic layer of an Oracle Alfa PDF."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    images: Dict[str, str] = {}
    image_xrefs: List[int] = []
    ff2 = b""
    ff2_raw = b""
    tounicode = b""
    content = b""
    content_raw = b""
    meta_xml = b""
    skel_parts: List[bytes] = []
    try:
        named = _xobject_map(doc)
        xref_to_im = {v: k for k, v in named.items()}
        for xref in range(1, doc.xref_length()):
            try:
                obj = doc.xref_object(xref) or ""
            except Exception:
                continue
            decoded = b""
            raw = b""
            try:
                decoded = doc.xref_stream(xref) or b""
                raw = doc.xref_stream_raw(xref) or b""
            except Exception:
                pass
            role = _role(obj, decoded)
            name = xref_to_im.get(xref, "")
            if role == "image" or name:
                key = name or f"img{xref}"
                images[key] = _sha256(decoded)
                image_xrefs.append(xref)
                skel_parts.append(f"IMG:{key}:{_sha256(decoded)}".encode("ascii"))
                continue
            if role == "font" or decoded[:4] == b"\x00\x01\x00\x00":
                ff2 = decoded
                ff2_raw = raw
                skel_parts.append(b"FONTFILE2")
                continue
            if role == "tounicode":
                tounicode = decoded
                skel_parts.append(b"TOUNICODE")
                continue
            if role == "content":
                content = decoded
                content_raw = raw
                stripped = _HEX_TJ_RE.sub(b"<>", decoded)
                skel_parts.append(b"CONTENT:" + hashlib.sha256(stripped).digest())
                continue
            if role == "metadata":
                meta_xml = decoded
                skel_parts.append(b"META")
                continue
            clean = _DATE_RE.sub(b"(D:*)", obj.encode("latin-1", "replace"))
            clean = _ID_RE.sub(b"/ID[<0><0>]", clean)
            skel_parts.append(f"{xref}:".encode("ascii") + clean)
        info = doc.metadata or {}
        trailer = ""
        try:
            trailer = doc.xref_object(-1) or ""
        except Exception:
            trailer = ""
    finally:
        doc.close()

    ids = _ID_RE.findall(pdf)
    id_pair = [t.decode("ascii").lower() for t in ids[0]] if ids else []
    producer_m = re.search(rb"/Producer\s*\((?:\\376\\377)?([^)]*)\)", pdf)
    creator_m = re.search(rb"/Creator\s*\((?:\\376\\377)?([^)]*)\)", pdf)
    meta = {
        "producer": (producer_m.group(1).decode("latin-1", "replace") if producer_m else ""),
        "creator": (creator_m.group(1).decode("latin-1", "replace") if creator_m else ""),
        "title": str(info.get("title") or ""),
        "creationDate": str(info.get("creationDate") or ""),
        "modDate": str(info.get("modDate") or ""),
    }
    return {
        "size": len(pdf),
        "md5": _md5(pdf),
        "sha256": _sha256(pdf),
        "images_decoded": {k: images[k] for k in sorted(images)},
        "content_raw": _sha256(content_raw),
        "content_decoded": _sha256(content),
        "fontfile2_raw": _sha256(ff2_raw),
        "fontfile2_decoded": _sha256(ff2),
        "tounicode": _sha256(tounicode),
        "skeleton": _sha256(b"\n".join(skel_parts)),
        "metadata": meta,
        "metadata_hash": _sha256(json.dumps(meta, sort_keys=True).encode("utf-8")),
        "id": id_pair,
        "image_xrefs": image_xrefs,
    }


def compare_layers(new: Dict[str, Any], donor: Dict[str, Any], prev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Images must match donor; Content / FontFile2 / ID must be new vs prev or donor."""
    img_ok = new.get("images_decoded") == donor.get("images_decoded")
    must_new = {}
    ref = prev or donor
    for key in ("content_decoded", "content_raw", "fontfile2_decoded", "id"):
        must_new[key] = new.get(key) != ref.get(key)
    return {
        "images_match_donor": img_ok,
        "dynamic_are_new": must_new,
        "ok": img_ok and all(must_new.values()),
    }


def dump_report(pdf: bytes, path: Optional[str] = None) -> Dict[str, Any]:
    rep = layer_report(pdf)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
    return rep
