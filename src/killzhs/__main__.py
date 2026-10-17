"""Application entry point, including PyInstaller's spawn bootstrap."""

import multiprocessing
import os
import sys
from pathlib import Path


def main():
    multiprocessing.freeze_support()
    for name in ("stdin", "stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "r" if name == "stdin" else "w"))
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    for tokenizer in (root / "tokenizer", root / ".local" / "tokenizer"):
        if tokenizer.is_dir():
            os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(tokenizer))
            break
    from .app.cli import main as serve
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
