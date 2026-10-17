"""PyInstaller bootstrap; application behavior lives in killzhs.__main__.main."""

import multiprocessing
import os
import sys
from pathlib import Path


def main():
    # PyInstaller intercepts spawned worker arguments here, before importing any
    # application code or opening a window. This also covers resource trackers.
    multiprocessing.freeze_support()
    if getattr(sys, "frozen", False):
        resources = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent / "_internal"))
        tokenizer = resources / "tokenizer"
        if not tokenizer.is_dir():
            raise RuntimeError("The bundled tokenizer cache is missing; extract the complete release ZIP.")
        os.environ["TIKTOKEN_CACHE_DIR"] = str(tokenizer)
    # --windowed intentionally supplies None for standard streams. Libraries
    # may still print diagnostics, including from spawned worker processes.
    for name in ("stdin", "stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "r" if name == "stdin" else "w", encoding="utf-8"))
    from killzhs.__main__ import main as application_main

    return application_main()


if __name__ == "__main__":
    raise SystemExit(main())
