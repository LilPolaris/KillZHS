"""Release entry point, also hosting isolated workers in the frozen executable."""

import io
import json
import os
from pathlib import Path
import sys


def main():
    tokenizer = Path(__file__).resolve().parent / "tokenizer"
    if tokenizer.is_dir():
        os.environ["TIKTOKEN_CACHE_DIR"] = str(tokenizer)
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        import webui_worker
        sys.stdin = io.StringIO(Path(sys.argv[2]).read_text(encoding="utf-8"))
        with Path(sys.argv[3]).open("w", encoding="utf-8", buffering=1) as output:
            webui_worker.OUTPUT = output
            return webui_worker.main()
    # PyInstaller --windowed supplies no console streams.
    for name in ("stdin", "stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "r" if name == "stdin" else "w"))
    from webui import main as serve
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
