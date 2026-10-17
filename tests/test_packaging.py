"""Offline release boundary and frozen-bootstrap checks; never invoke PyInstaller."""

import hashlib
import json
import sys
import zipfile
from importlib import metadata
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from scripts import build_windows, dependency_tools, frozen_entry


def test_pyinstaller_uses_src_entry_onedir_windowed_and_explicit_public_assets(tmp_path):
    arguments = build_windows.pyinstaller_arguments(tmp_path, tmp_path / "stage", tmp_path / "dist")
    assert arguments[0] == str(tmp_path / "scripts" / "frozen_entry.py")
    assert arguments[arguments.index("--paths") + 1] == str(tmp_path / "src")
    assert "--onedir" in arguments and "--windowed" in arguments and "--onefile" not in arguments
    assets = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--add-data"]
    assert {asset.rsplit(":", 1)[-1] for asset in assets} == {"web", ".", "tokenizer", "third-party-licenses"}
    assert "killzhs.__main__" in arguments and "tiktoken_ext" in arguments and "Crypto" in arguments
    assert all(".local" not in asset for asset in assets)


def test_project_version_is_read_without_importing_package(tmp_path):
    package = tmp_path / "src" / "killzhs"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('VERSION = "0.2.0"\nraise RuntimeError("must not import")\n', encoding="utf-8")
    assert build_windows.project_version(tmp_path) == "0.2.0"


def test_tokenizer_rejects_missing_or_corrupt_data_without_attempting_download(tmp_path):
    with pytest.raises(RuntimeError, match="Missing offline"):
        build_windows.tokenizer_file(tmp_path)
    (tmp_path / build_windows.TOKENIZER_NAME).write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        build_windows.tokenizer_file(tmp_path)


@pytest.mark.parametrize("relative", [
    "cookies.json", "config.json", "nested/HISTORY.JSON", "nested/.env", "nested/.env.production",
    ".local/secret", "task-logs/task.txt", "assessment-attempts/attempt.json", "instance.lock",
])
def test_release_scan_rejects_runtime_files(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic test value", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Private/runtime"):
        build_windows.assert_public_tree(tmp_path)


def test_release_scan_rejects_links(tmp_path, monkeypatch):
    asset = tmp_path / "index.html"
    asset.write_text("public", encoding="utf-8")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == asset or original(path))
    with pytest.raises(RuntimeError, match="Links"):
        build_windows.assert_public_tree(tmp_path)


def test_license_collection_preserves_nested_paths_and_inventory(tmp_path):
    installed = tmp_path / "installed"
    license_path = installed / "example-1.0.dist-info" / "licenses" / "LICENSE"
    license_path.parent.mkdir(parents=True)
    license_path.write_text("Synthetic license", encoding="utf-8")
    runtime = {
        "example": SimpleNamespace(
            version="1.0", files=[metadata.PackagePath("example-1.0.dist-info/licenses/LICENSE")],
            locate_file=lambda item: installed / item,
        ),
        "no-text": SimpleNamespace(version="2.0", files=[], locate_file=lambda item: installed / item),
    }
    destination = tmp_path / "licenses"
    inventory = build_windows.collect_licenses(runtime, destination)
    assert (destination / "example" / "licenses" / "LICENSE").read_text(encoding="utf-8") == "Synthetic license"
    assert inventory == [
        {"name": "example", "version": "1.0", "license_files": ["licenses/LICENSE"]},
        {"name": "no-text", "version": "2.0", "license_files": []},
    ]
    assert json.loads((destination / "inventory.json").read_text(encoding="utf-8")) == inventory


def test_archive_has_one_top_level_directory_checksum_and_never_overwrites(tmp_path):
    bundle = tmp_path / "KillZHS"
    bundle.mkdir()
    (bundle / "LICENSE").write_text("Synthetic license", encoding="utf-8")
    archive = tmp_path / "release.zip"
    digest = build_windows.create_archive(bundle, archive)
    with zipfile.ZipFile(archive) as output:
        assert output.namelist() == ["KillZHS/LICENSE"]
    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        build_windows.create_archive(bundle, archive)


def test_check_never_runs_pyinstaller_or_creates_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build_windows, "ROOT", tmp_path)
    monkeypatch.setattr(build_windows, "preflight", lambda root, cache: ("0.2.0", {"root": object()}, cache))
    assert build_windows.main(["--check"]) == 0
    assert list(tmp_path.iterdir()) == []
    assert "Ready:" in capsys.readouterr().out


@pytest.mark.parametrize("missing", [path for path in build_windows.RELEASE_INPUTS if path.startswith("src/killzhs/app/")])
def test_preflight_rejects_missing_application_modes(tmp_path, monkeypatch, missing):
    for relative in build_windows.RELEASE_INPUTS:
        if relative != missing:
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("pass\n" if relative.endswith(".py") else "public fixture", encoding="utf-8")
    monkeypatch.setattr(build_windows, "enable_build_tools", lambda root: None)
    monkeypatch.setattr(build_windows, "validate_lock", lambda path: {})
    with pytest.raises(RuntimeError, match=f"Missing release input: {missing}"):
        build_windows.preflight(tmp_path, tmp_path / "cache")


def test_missing_project_imports_fail_after_pyinstaller_analysis(tmp_path):
    warnings = tmp_path / "work" / "KillZHS" / "warn-KillZHS.txt"
    warnings.parent.mkdir(parents=True)
    warnings.write_text("missing module named numpy - imported by tiktoken.core (optional)\n", encoding="utf-8")
    build_windows.assert_application_imports(tmp_path)
    with warnings.open("a", encoding="utf-8") as output:
        output.write("missing module named 'killzhs.app.demo' - imported by killzhs.app.controller\n")
    with pytest.raises(RuntimeError, match="Incomplete application bundle; missing modules: killzhs.app.demo"):
        build_windows.assert_application_imports(tmp_path)


def test_frozen_archive_must_contain_required_package_and_modules(tmp_path, monkeypatch):
    modules = {"killzhs": (), "killzhs.app": (), "killzhs.app.actions": (), "killzhs.ai": (), "killzhs.ai.providers": ()}
    reader = SimpleNamespace(
        toc={"PYZ.pyz": (0, 0, 0, 0, "z")}, open_embedded_archive=lambda name: SimpleNamespace(toc=modules),
    )
    fake_readers = ModuleType("PyInstaller.archive.readers")
    fake_readers.CArchiveReader = lambda name: reader
    monkeypatch.setitem(sys.modules, "PyInstaller.archive.readers", fake_readers)
    monkeypatch.setattr(build_windows, "RELEASE_INPUTS", (
        "src/killzhs/__init__.py", "src/killzhs/app/__init__.py", "src/killzhs/app/actions.py",
        "src/killzhs/ai/providers.py",
    ))
    build_windows.assert_frozen_modules(tmp_path / "KillZHS.exe")
    del modules["killzhs.ai"]
    with pytest.raises(RuntimeError, match="missing required application modules: killzhs.ai"):
        build_windows.assert_frozen_modules(tmp_path / "KillZHS.exe")
    modules["killzhs.ai"] = ()
    del modules["killzhs.app.actions"]
    with pytest.raises(RuntimeError, match="missing required application modules: killzhs.app.actions"):
        build_windows.assert_frozen_modules(tmp_path / "KillZHS.exe")


def test_local_build_tools_are_fallback_only_and_never_write_legacy_bytecode(tmp_path, monkeypatch):
    root = tmp_path / "KillZHS"
    legacy_tools = tmp_path / "fuckZHS" / ".local" / "build-tools"
    (legacy_tools / "PyInstaller").mkdir(parents=True)
    (legacy_tools / "PyInstaller" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(dependency_tools.util, "find_spec", lambda name: object())
    assert dependency_tools.enable_build_tools(root) is None
    monkeypatch.setattr(dependency_tools.util, "find_spec", lambda name: None)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    assert dependency_tools.enable_build_tools(root) == legacy_tools
    assert sys.path[-1] == str(legacy_tools)
    assert sys.dont_write_bytecode
    assert dependency_tools.os.environ["PYTHONDONTWRITEBYTECODE"] == "1"


def test_missing_build_tools_fail_without_install(tmp_path, monkeypatch):
    monkeypatch.setattr(dependency_tools.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="No installation attempted"):
        dependency_tools.enable_build_tools(tmp_path / "KillZHS")


def test_frozen_bootstrap_freezes_before_app_import_and_sets_bundled_cache(tmp_path, monkeypatch):
    resources = tmp_path / "_internal"
    (resources / "tokenizer").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(frozen_entry.multiprocessing, "freeze_support", lambda: calls.append("freeze"))
    module = ModuleType("killzhs.__main__")

    def application_main():
        assert calls == ["freeze"]
        assert frozen_entry.os.environ["TIKTOKEN_CACHE_DIR"] == str(resources / "tokenizer")
        return 17

    module.main = application_main
    monkeypatch.setitem(sys.modules, "killzhs.__main__", module)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(resources), raising=False)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "ignored-external-cache")
    assert frozen_entry.main() == 17


def test_frozen_bootstrap_aborts_when_bundle_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(frozen_entry.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(RuntimeError, match="bundled tokenizer cache is missing"):
        frozen_entry.main()
