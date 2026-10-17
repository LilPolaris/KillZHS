"""Build a Windows onedir ZIP from public assets and installed dependencies, offline.

Use --check for a read-only preflight. A build never downloads tokenizer data or
installs tools. --tokenizer-cache can select an existing public tiktoken cache.
"""

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import zipfile
from importlib import metadata
from pathlib import Path

if __package__:
    from .dependency_tools import enable_build_tools, validate_lock
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dependency_tools import enable_build_tools, validate_lock

ROOT = Path(__file__).resolve().parents[1]
TOKENIZER_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
TOKENIZER_NAME = hashlib.sha1(TOKENIZER_URL.encode(), usedforsecurity=False).hexdigest()
TOKENIZER_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
LICENSE_PREFIXES = ("license", "licence", "copying", "notice", "copyright")
FORBIDDEN_NAMES = {
    ".git", ".venv", ".local", ".env", "__pycache__", "logs", "task-logs", "assessment-attempts", "ai-answers",
    "config.json", "courses.json", "cookies.json", "execution.json", "ai.json", "settings.json", "history.json",
    "notifications.json", "active-task.json", "instance.json", "instance.lock", "schedule-state.json",
}
RELEASE_INPUTS = (
    "scripts/frozen_entry.py", "scripts/launch_verification_probe.bat", "src/killzhs/__init__.py",
    "src/killzhs/__main__.py", "src/killzhs/models.py", "src/killzhs/runtime.py", "src/killzhs/privacy.py",
    "src/killzhs/notifications.py",
    *(f"src/killzhs/app/{name}.py" for name in (
        "__init__", "actions", "cli", "controller", "demo", "desktop", "history", "scheduler", "server", "store",
    )),
    *(f"src/killzhs/worker/{name}.py" for name in ("__init__", "jobs", "process", "protocol")),
    *(f"src/killzhs/platform/{name}.py" for name in (
        "http", "crypto", "auth", "base", "shared", "hike", "ai_course", "assessments", "verification",
    )),
    "src/killzhs/ai/providers.py", "src/killzhs/ai/ppt.py",
    "LICENSE", "NOTICE.md", "web/index.html", "docs/WINDOWS.md",
)


def project_version(root=ROOT):
    """Read the public version constant without importing the application."""
    tree = ast.parse((root / "src" / "killzhs" / "__init__.py").read_text(encoding="utf-8"))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VERSION" for target in statement.targets
        ):
            version = ast.literal_eval(statement.value)
            if isinstance(version, str) and version and all(char.isalnum() or char in ".-+" for char in version):
                return version
    raise RuntimeError("src/killzhs/__init__.py must define a safe literal VERSION")


def tokenizer_file(cache):
    path = Path(cache) / TOKENIZER_NAME
    if not path.is_file():
        raise RuntimeError(f"Missing offline cl100k_base cache in {cache}; no download attempted")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != TOKENIZER_SHA256:
        raise RuntimeError("Offline cl100k_base cache checksum mismatch; no download attempted")
    return path


def assert_public_tree(directory):
    """Reject private/runtime paths and links before copying or archiving assets."""
    for path in (directory, *directory.rglob("*")):
        relative = path.relative_to(directory)
        if path.is_symlink() or path.is_junction():
            raise RuntimeError(f"Links are not allowed in release assets: {relative}")
        if any(part.lower() in FORBIDDEN_NAMES or part.lower().startswith(".env.") for part in relative.parts):
            raise RuntimeError(f"Private/runtime path unexpectedly included in release: {relative}")


def collect_licenses(runtime, destination):
    """Preserve runtime dist-info license texts and an inventory of their origins."""
    destination.mkdir(parents=True, exist_ok=True)
    inventory = []
    for name, distribution in sorted(runtime.items()):
        files = []
        for item in distribution.files or []:
            if not item.name.lower().startswith(LICENSE_PREFIXES):
                continue
            parts = item.parts
            info_index = next((index for index, part in enumerate(parts) if part.endswith(".dist-info")), None)
            if info_index is None:
                continue
            relative = Path(*parts[info_index + 1:])
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"Unsafe license path for {name}")
            source = Path(distribution.locate_file(item))
            if not source.is_file():
                continue
            target = destination / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files.append(relative.as_posix())
        inventory.append({"name": name, "version": distribution.version, "license_files": sorted(files)})
    (destination / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return inventory


def collect_platform_licenses(destination, *, python_root=None, lookup=metadata.distribution):
    """Ship interpreter terms and the frozen bootloader's full license/exception."""
    python_license = Path(python_root or sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("The Python interpreter license is missing")
    bootloader = lookup("pyinstaller")
    copying = next((item for item in bootloader.files or [] if item.name == "COPYING.txt"), None)
    bootloader_license = Path(bootloader.locate_file(copying)) if copying is not None else None
    if bootloader_license is None or not bootloader_license.is_file():
        raise RuntimeError("The PyInstaller bootloader license is missing")
    rows = []
    for name, version, source in (
        ("cpython", sys.version.split()[0], python_license),
        ("pyinstaller-bootloader", bootloader.version, bootloader_license),
    ):
        target = destination / name / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        rows.append({"name": name, "version": version, "license_file": target.relative_to(destination).as_posix()})
    (destination / "components.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return rows


def pyinstaller_arguments(root, staging, bundle_parent):
    return [
        str(root / "scripts" / "frozen_entry.py"),
        "--name", "KillZHS", "--onedir", "--windowed", "--noconfirm",
        "--paths", str(root / "src"),
        "--distpath", str(bundle_parent), "--workpath", str(staging / "work"), "--specpath", str(staging),
        "--add-data", f"{root / 'web'}:web",
        "--add-data", f"{root / 'LICENSE'}:.", "--add-data", f"{root / 'NOTICE.md'}:.",
        "--add-data", f"{staging / 'tokenizer'}:tokenizer",
        "--add-data", f"{staging / 'third-party-licenses'}:third-party-licenses",
        "--hidden-import", "killzhs.__main__", "--collect-submodules", "tiktoken_ext", "--collect-all", "Crypto",
        "--exclude-module", "pytest", "--exclude-module", "playwright", "--exclude-module", "IPython",
    ]


def preflight(root, cache):
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise RuntimeError("Build the Windows x64 package with a 64-bit Windows interpreter")
    if sys.version_info[:2] not in ((3, 12), (3, 14)):
        raise RuntimeError("Use Python 3.12 or 3.14 for the Windows release")
    enable_build_tools(root)
    runtime = validate_lock(root / "requirements-lock.txt")
    validate_lock(root / "requirements-build-lock.txt")
    for relative in RELEASE_INPUTS:
        if not (root / relative).is_file():
            raise RuntimeError(f"Missing release input: {relative}")
        if relative.endswith(".py"):
            ast.parse((root / relative).read_text(encoding="utf-8"), filename=relative)
    assert_public_tree(root / "web")
    tokenizer = tokenizer_file(cache)
    return project_version(root), runtime, tokenizer


def assert_application_imports(staging):
    """Optional-library warnings are normal; missing project modules are fatal."""
    warnings = staging / "work" / "KillZHS" / "warn-KillZHS.txt"
    missing = re.findall(
        r"^missing module named ['\"]?(killzhs(?:\.[\w]+)*)",
        warnings.read_text(encoding="utf-8"), re.MULTILINE,
    )
    if missing:
        raise RuntimeError("Incomplete application bundle; missing modules: " + ", ".join(sorted(set(missing))))


def assert_frozen_modules(executable):
    """Inspect the actual frozen module table instead of trusting hook warnings."""
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    embedded = next(name for name, entry in archive.toc.items() if entry[-1] == "z")
    modules = archive.open_embedded_archive(embedded).toc
    required = set()
    for relative in RELEASE_INPUTS:
        path = Path(relative)
        if path.parts[0] == "src" and path.suffix == ".py":
            parts = path.with_suffix("").parts[1:]
            if parts[-1] == "__init__":
                parts = parts[:-1]
            required.update(".".join(parts[:count]) for count in range(1, len(parts) + 1))
    missing = required - modules.keys()
    if missing:
        raise RuntimeError("Frozen archive is missing required application modules: " + ", ".join(sorted(missing)))


def create_archive(bundle, archive):
    assert_public_tree(bundle)
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(bundle.parent))
    with archive.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read-only readiness check; never invokes PyInstaller")
    parser.add_argument("--tokenizer-cache", type=Path, default=ROOT / ".local" / "tokenizer")
    args = parser.parse_args(argv)
    version, runtime, tokenizer = preflight(ROOT, args.tokenizer_cache)
    if args.check:
        print(f"Ready: KillZHS {version}, {len(runtime)} runtime distributions, verified offline tokenizer.")
        return 0
    # Isolate every attempt. Existing bundles, archives, and running releases are
    # never removed or overwritten, including repeated builds of one version.
    build_root = ROOT / ".local" / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=build_root))
    release_root = ROOT / "output" / "release"
    release_root.mkdir(parents=True, exist_ok=True)
    release = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=release_root))
    cache = staging / "tokenizer"
    cache.mkdir()
    shutil.copyfile(tokenizer, cache / TOKENIZER_NAME)
    collect_licenses(runtime, staging / "third-party-licenses")
    collect_platform_licenses(staging / "third-party-licenses")
    # Make accidental future tokenizer lookups use only the verified staged data.
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
    from PyInstaller.__main__ import run

    run(pyinstaller_arguments(ROOT, staging, release))
    assert_application_imports(staging)
    bundle = release / "KillZHS"
    assert_frozen_modules(bundle / "KillZHS.exe")
    for source, target in (
        (ROOT / "LICENSE", "LICENSE"), (ROOT / "NOTICE.md", "NOTICE.md"),
        (ROOT / "scripts" / "launch_verification_probe.bat", "验证实验模式.bat"),
    ):
        shutil.copyfile(source, bundle / target)
    shutil.copyfile(ROOT / "docs" / "WINDOWS.md", bundle / "使用说明.txt")
    archive = release / f"KillZHS-{version}-windows-x64.zip"
    checksum = create_archive(bundle, archive)
    (release / "SHA256SUMS.txt").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    manifest = {"version": version, "bundle": str(bundle), "archive": str(archive), "sha256": checksum}
    (release / "build.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (release_root / "latest-build.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Release ready: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
