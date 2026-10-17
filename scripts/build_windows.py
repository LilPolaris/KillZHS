"""Build a clean Windows release using explicit public assets only."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from killzhs_config import VERSION


def main():
    if sys.platform != "win32":
        raise SystemExit("Build the Windows package on Windows.")
    local_tools = ROOT / ".local" / "build-tools"
    if local_tools.is_dir():
        sys.path.insert(0, str(local_tools))
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit("Install requirements-build.txt in the build environment first.")
    from dependency_tools import dependency_closure, requirement_lines
    runtime = dependency_closure(requirement_lines(ROOT / "requirements-lock.txt"))
    dependency_closure(requirement_lines(ROOT / "requirements-build-lock.txt"))
    build = ROOT / ".local" / "build"
    release = ROOT / "output" / "release"
    # A user may already be testing the previous build of this version. Never
    # let PyInstaller --noconfirm recursively remove an existing distribution.
    release.mkdir(parents=True, exist_ok=True)
    distribution_dir = Path(tempfile.mkdtemp(prefix=VERSION + "-", dir=release))
    tokenizer = build / "tokenizer"
    tokenizer.mkdir(parents=True, exist_ok=True)
    release.mkdir(parents=True, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(tokenizer)
    import tiktoken
    tiktoken.get_encoding("cl100k_base")
    # Collect license files from the installed runtime distributions.
    licenses = Path(tempfile.mkdtemp(prefix="third-party-licenses-", dir=build))
    inventory = []
    for name, distribution in sorted(runtime.items()):
        files = []
        for item in distribution.files or []:
            if item.name.lower().startswith(("license", "licence", "copying", "notice", "copyright")) and ".dist-info" in str(item):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    info_index = next(i for i, part in enumerate(item.parts) if part.endswith(".dist-info"))
                    relative = Path(*item.parts[info_index + 1:])
                    target = licenses / name / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    files.append(relative.as_posix())
        inventory.append({"name": name, "version": distribution.version, "license_files": files})
    (licenses / "inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    from PyInstaller.__main__ import run
    run([str(ROOT / "killzhs.py"), "--name", "KillZHS", "--onedir", "--windowed", "--noconfirm",
         "--distpath", str(distribution_dir), "--workpath", str(build / "work"), "--specpath", str(build),
         "--add-data", str(ROOT / "web") + ":web", "--add-data", str(ROOT / "LICENSE") + ":.",
         "--add-data", str(ROOT / "NOTICE.md") + ":.", "--add-data", str(tokenizer) + ":tokenizer",
         "--add-data", str(licenses) + ":third-party-licenses", "--collect-submodules", "tiktoken_ext",
         "--collect-all", "Crypto", "--exclude-module", "pytest", "--exclude-module", "IPython"])
    bundle = distribution_dir / "KillZHS"
    shutil.copyfile(ROOT / "LICENSE", bundle / "LICENSE")
    shutil.copyfile(ROOT / "NOTICE.md", bundle / "NOTICE.md")
    shutil.copyfile(ROOT / "docs" / "WINDOWS.md", bundle / "使用说明.txt")
    shutil.copyfile(ROOT / "scripts" / "launch_verification_probe.bat", bundle / "验证实验模式.bat")
    forbidden = {"config.json", "cookies.json", "execution.json", "ai.json", "settings.json", "history.json",
                 "notifications.json", "active-task.json", "instance.json", ".env"}
    for path in bundle.rglob("*"):
        if path.name in forbidden or path.name in (".git", ".venv", ".local", "logs"):
            raise RuntimeError("Private/runtime file unexpectedly included in release: " + path.name)
    archive = release / f"KillZHS-{VERSION}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(distribution_dir))
    checksum = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
    (release / "SHA256SUMS.txt").write_text(checksum + "  " + archive.name + "\n", encoding="utf-8")
    (release / "latest-build.json").write_text(json.dumps({"version": VERSION,
        "bundle": str(bundle), "archive": str(archive), "sha256": checksum}, indent=2), encoding="utf-8")
    print(f"Release ready: {archive.name} ({archive.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
