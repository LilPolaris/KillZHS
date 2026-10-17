"""A release includes the interpreter and bootloader, not just pip libraries."""

import json
from importlib import metadata
from types import SimpleNamespace

import pytest
from scripts.build_windows import collect_platform_licenses


def test_interpreter_and_bootloader_notices_are_preserved(tmp_path):
    interpreter = tmp_path / "python"
    interpreter.mkdir()
    (interpreter / "LICENSE.txt").write_text("CPython complete terms", encoding="utf-8")
    copying = metadata.PackagePath("pyinstaller.dist-info/licenses/COPYING.txt")
    path = tmp_path / copying
    path.parent.mkdir(parents=True)
    path.write_text("GPL and Bootloader Exception complete terms", encoding="utf-8")
    distribution = SimpleNamespace(files=[copying], version="6.7", locate_file=lambda item: tmp_path / item)
    destination = tmp_path / "notices"
    rows = collect_platform_licenses(destination, python_root=interpreter, lookup=lambda _: distribution)
    assert {row["name"] for row in rows} == {"cpython", "pyinstaller-bootloader"}
    assert (destination / "cpython/LICENSE.txt").read_text() == "CPython complete terms"
    assert "Bootloader Exception" in (destination / "pyinstaller-bootloader/COPYING.txt").read_text()
    assert json.loads((destination / "components.json").read_text()) == rows
    assert str(tmp_path) not in (destination / "components.json").read_text()


def test_missing_notices_block_publication(tmp_path):
    with pytest.raises(RuntimeError, match="Python interpreter"):
        collect_platform_licenses(tmp_path / "out", python_root=tmp_path)
    (tmp_path / "LICENSE.txt").write_text("Python terms", encoding="utf-8")
    with pytest.raises(RuntimeError, match="bootloader"):
        collect_platform_licenses(tmp_path / "out", python_root=tmp_path,
                                  lookup=lambda _: SimpleNamespace(files=[]))
