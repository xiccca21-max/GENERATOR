"""
Проверяет все PDF в папке через Telegram-бот чекер.
Пример:
  python tools/tg_check_batch.py output/fix4_test
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent

# импорт из соседнего скрипта
sys.path.insert(0, str(_DIR))
from tg_check_pdf import _load_env, check_one_pdf  # noqa: E402


async def run_batch(folder: Path, pause: float) -> int:
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        print(f"В папке нет PDF: {folder}")
        return 1

    cfg = _load_env()
    fails = 0

    print(f"Файлов: {len(pdfs)}")
    print("=" * 60)

    for i, pdf in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf.name}")
        result = await check_one_pdf(pdf, cfg)
        print(result)
        print("-" * 60)
        if result.startswith("FAIL"):
            fails += 1
        if i < len(pdfs):
            await asyncio.sleep(pause)

    print(f"Итого: PASS={len(pdfs)-fails} FAIL={fails}")
    return 0 if fails == 0 else 2


def main() -> None:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", help="Папка с PDF")
    parser.add_argument("--pause", type=float, default=3.0, help="Пауза между файлами (сек)")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_absolute():
        folder = (_ROOT / folder).resolve()

    code = asyncio.run(run_batch(folder, args.pause))
    sys.exit(code)


if __name__ == "__main__":
    main()
