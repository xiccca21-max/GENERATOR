"""Корпус оригиналов Альфа-Банка — классификация подметодов."""
from __future__ import annotations

import os
import re
import statistics
from typing import Dict, List, Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
_CORPUS_CANDIDATES = [
    os.environ.get("ALFA_CORPUS_DIR") or "",
    os.path.join(_DIR, "templates", "alfa_corpus"),
    os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "альфа"),
]


def _resolve_corpus_dir() -> str:
    for path in _CORPUS_CANDIDATES:
        if path and os.path.isdir(path):
            return path
    return _CORPUS_CANDIDATES[-1]


CORPUS_DIR = _resolve_corpus_dir()

_KIND_LABELS = {
    "sbp": "СБП",
    "card": "Карта на карту",
    "phone": "По телефону (Альфа→Альфа)",
    "unknown": "неизвестно",
}

_SIZE_TARGET: Dict[str, int] = {
    "sbp": 58800,
    "card": 55326,
    "phone": 69706,
}


def classify_lines(lines: List[str], filename: str = "") -> str:
    text = "\n".join(lines).replace("\xa0", " ")
    low = filename.lower()
    low_text = text.lower()
    if "клиенту альфа" in low_text or "по номеру телефона" in low or "телефон" in low and "альфа" in low:
        if "сбп" not in low_text and "на карту" not in low_text:
            return "phone"
    if "карт" in low_text and "на карту" in low_text:
        return "card"
    if "карт" in low and ("на карту" in low or "карта" in low):
        return "card"
    if "сбп" in low_text or low.startswith("сбп") or "альфа сбп" in low:
        return "sbp"
    if "переводе по сбп" in low_text or "Идентификатор операции в СБП" in text:
        return "sbp"
    if "квитанция о переводе клиенту альфа-банка" in low_text:
        return "phone"
    return "unknown"


def classify_pdf(path: str) -> str:
    import fitz

    doc = fitz.open(path)
    lines = [l.strip() for l in doc[0].get_text().split("\n") if l.strip()]
    doc.close()
    return classify_lines(lines, os.path.basename(path))


def _content_xref(path: str) -> Optional[int]:
    try:
        import fitz

        doc = fitz.open(path)
        xref = doc[0].get_contents()[0]
        doc.close()
        return int(xref)
    except Exception:
        return None


def is_canonical(path: str) -> bool:
    """Oracle BI (sbp/card): content xref 9, ~55–59 KB.
    Phone (iOS Quartz): content xref 3, ~69 KB.
    """
    if not path or not os.path.isfile(path):
        return False
    size = os.path.getsize(path)
    xref = _content_xref(path)
    kind = None
    try:
        kind = classify_pdf(path)
    except Exception:
        kind = None
    if kind == "phone":
        return xref == 3 and 65000 <= size <= 75000
    if xref != 9:
        return False
    return 54000 <= size <= 64000


def canonical_paths(kind: str) -> List[str]:
    return [p for p in corpus_paths(kind) if is_canonical(p)]


def corpus_paths(kind: str) -> List[str]:
    if not os.path.isdir(CORPUS_DIR):
        return []
    out: List[str] = []
    for name in sorted(os.listdir(CORPUS_DIR)):
        if not name.lower().endswith(".pdf"):
            continue
        path = os.path.join(CORPUS_DIR, name)
        try:
            if classify_pdf(path) == kind:
                out.append(path)
        except Exception:
            pass
    return out


def median_size(kind: str, fallback: Optional[int] = None) -> int:
    paths = canonical_paths(kind) or corpus_paths(kind)
    if paths:
        sizes = [os.path.getsize(p) for p in paths]
        med = int(statistics.median(sizes))
        _SIZE_TARGET[kind] = med
        return med
    return _SIZE_TARGET.get(kind) or fallback or 58800


def template_paths(kind: str, default_template: str) -> List[str]:
    seen = set()
    out: List[str] = []
    for p in [default_template] + corpus_paths(kind):
        if p and os.path.isfile(p) and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def pick_donor(kind: str, op_date: Optional[str] = None) -> Optional[str]:
    paths = rank_donors(kind)
    if not paths:
        return None
    if op_date:
        norm = lambda s: re.sub(r"\s+", " ", s.strip())
        want = norm(op_date)
        for p in paths:
            try:
                import fitz
                doc = fitz.open(p)
                lines = [l.strip() for l in doc[0].get_text().split("\n") if l.strip()]
                doc.close()
                for i, l in enumerate(lines):
                    if "Дата и время перевода" in l and i + 1 < len(lines):
                        if norm(lines[i + 1].replace("мск", "").strip()) == want.replace(" мск", "").strip():
                            return p
            except Exception:
                pass
    return paths[0]


def has_compact_w_array(path: str) -> bool:
    """Оригинальные Alfa PDF: /W без пробелов между CID (не pretty-printed)."""
    try:
        with open(path, "rb") as fp:
            raw = fp.read()
        return not bool(re.search(rb"/W\s*\[\s*\d+\s+\[", raw))
    except OSError:
        return False


def rank_donors(kind: str) -> List[str]:
    """Доноры: compact /W, крупные слоты, богатый charset."""
    paths = canonical_paths(kind) or corpus_paths(kind)
    if not paths:
        return []

    def score(p: str) -> tuple:
        try:
            import fitz
            from alfa_orig_mode import AlfaOrigContext

            ctx = AlfaOrigContext()
            if not ctx.load(p):
                return (0, 0, 0, os.path.getsize(p))
            bank = 0
            if kind == "sbp":
                bank = ctx.slot_size_at(621.4, 304.75)
            elif kind == "card":
                dt = ctx.slot_size_at(621.4, 304.75)
                op = ctx.slot_size_at(578.5, 304.75)
                bank = min(dt, op)
            elif kind == "phone":
                from alfa_phone_orig_mode import AlfaPhoneOrigContext

                pctx = AlfaPhoneOrigContext()
                if not pctx.load(p):
                    return (0, 0, 0, os.path.getsize(p))
                bank = pctx.slot_size_at(621.394, 304.75)
                return (
                    1,
                    bank,
                    len(pctx.available_chars),
                    -abs(os.path.getsize(p) - _SIZE_TARGET.get(kind, 69706)),
                )
            compact = 1 if has_compact_w_array(p) else 0
            return (
                compact,
                bank,
                len(ctx.available_chars),
                -abs(os.path.getsize(p) - _SIZE_TARGET.get(kind, 58800)),
            )
        except Exception:
            return (0, 0, 0, os.path.getsize(p))

    return sorted(paths, key=score, reverse=True)
