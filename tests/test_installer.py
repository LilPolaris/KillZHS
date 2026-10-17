"""Installer contract and failure boundaries; no real compiler or installation."""

import hashlib
import json
import struct
import subprocess
from pathlib import Path

import pytest
from scripts import build_installer


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "工程 with spaces"
    bundle = root / "release" / "KillZHS"
    (bundle / "_internal").mkdir(parents=True)
    executable = bytearray(256)
    executable[:2] = b"MZ"
    struct.pack_into("<I", executable, 0x3C, 128)
    executable[128:134] = b"PE\0\0\x64\x86"
    (bundle / "KillZHS.exe").write_bytes(executable)
    (bundle / "_internal" / "python314.dll").write_bytes(b"synthetic runtime")
    archive = bundle.parent / "KillZHS-0.2.1-windows-x64.zip"
    archive.write_bytes(b"synthetic ZIP; digest validation only")
    manifest = bundle.parent / "build.json"
    metadata = {
        "version": "0.2.1", "bundle": str(bundle), "archive": str(archive),
        "sha256": build_installer.sha256(archive), "extra_metadata": {"keep": True},
    }
    manifest.write_text(json.dumps(metadata), encoding="utf-8")
    compiler = root / "tools" / "inno" / "ISCC.exe"
    language = compiler.parent / "Languages" / "ChineseSimplified.isl"
    language.parent.mkdir(parents=True)
    compiler.write_bytes(b"synthetic compiler; must not be executed")
    language.write_bytes(b"synthetic upstream language")
    monkeypatch.setattr(build_installer, "CHINESE_SHA256", build_installer.sha256(language))
    template = root / "packaging" / "windows.iss"
    template.parent.mkdir()
    template.write_text("synthetic template", encoding="utf-8")
    monkeypatch.setattr(build_installer, "ROOT", root)
    return root, manifest, bundle, compiler, metadata


def ready(source):
    root, manifest, bundle, compiler, _ = source
    return build_installer.preflight(manifest, bundle, compiler, root=root)


def fake_compiler(monkeypatch, source, mode="success"):
    def run(arguments, *, check, shell):
        assert check and shell is False
        output = Path(next(value[2:] for value in arguments if value.startswith("/O")))
        if mode == "failed":
            raise subprocess.CalledProcessError(2, arguments)
        if mode == "no-output":
            return
        (output / "KillZHS-0.2.1-windows-x64-setup.exe").write_bytes(b"synthetic installer")
        if mode == "changed-payload":
            (source[2] / "KillZHS.exe").write_bytes(b"concurrent change")
        if mode == "changed-manifest":
            metadata = source[4] | {"version": "0.2.2"}
            source[1].write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(build_installer.subprocess, "run", run)
    monkeypatch.setattr(build_installer.sys, "platform", "win32")


def test_check_is_read_only_even_with_update_flag(source, monkeypatch, capsys):
    root, manifest, bundle, compiler, _ = source
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(build_installer.subprocess, "run", lambda *args, **kwargs: pytest.fail("ISCC invoked"))
    assert build_installer.main([
        "--check", "--update-manifest", "--bundle", str(bundle), "--iscc", str(compiler),
    ]) == 0
    after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert before == after
    assert not (root / "output").exists()
    assert "Ready: KillZHS 0.2.1" in capsys.readouterr().out
    assert manifest.read_bytes() == before[manifest]


def test_compiler_arguments_preserve_chinese_spaces_and_metadata_version(source):
    inputs = ready(source)
    output = source[0] / "unique installer stage"
    arguments = build_installer.compiler_arguments(inputs, output)
    assert arguments == [
        str(inputs.iscc), "/Q", f"/O{output}", "/DAppVersion=0.2.1",
        f"/DBundleDir={inputs.bundle}", f"/DChineseLanguageFile={inputs.language}", str(inputs.template),
    ]


def test_build_is_unique_hashes_installer_and_preserves_base_artifacts(source, monkeypatch):
    root, manifest, _, _, metadata = source
    inputs = ready(source)
    before = manifest.read_bytes()
    base_checksums = manifest.parent / "SHA256SUMS.txt"
    base_checksums.write_text("preserve ZIP checksum file\n", encoding="utf-8")
    fake_compiler(monkeypatch, source)
    first = build_installer.build(inputs, root / "installers")
    second = build_installer.build(inputs, root / "installers")
    assert first.parent != second.parent
    assert manifest.read_bytes() == before
    assert base_checksums.read_text(encoding="utf-8") == "preserve ZIP checksum file\n"
    result = json.loads(first.read_text(encoding="utf-8"))
    assert {key: result[key] for key in metadata} == metadata
    installer = Path(result["installer"])
    assert result["installer_sha256"] == hashlib.sha256(installer.read_bytes()).hexdigest()
    assert Path(result["installer_checksums"]).read_text(encoding="utf-8") == (
        f"{result['installer_sha256']}  {installer.name}\n"
    )
    assert result["installer_build"]["source_manifest"] == str(manifest)


def test_explicit_manifest_update_adds_fields_without_changing_zip_digest(source, monkeypatch):
    root, manifest, _, _, metadata = source
    inputs = ready(source)
    fake_compiler(monkeypatch, source)
    generated = build_installer.build(inputs, root / "installers", update_source=True)
    updated = json.loads(manifest.read_text(encoding="utf-8"))
    assert updated == json.loads(generated.read_text(encoding="utf-8"))
    assert {key: updated[key] for key in metadata} == metadata
    assert updated["sha256"] != updated["installer_sha256"]


@pytest.mark.parametrize("mode, message", [
    ("failed", None), ("no-output", "expected installer"),
    ("changed-payload", "bundle changed"), ("changed-manifest", "metadata changed"),
])
def test_compiler_failures_and_concurrent_changes_do_not_replace_base_metadata(source, monkeypatch, mode, message):
    root, manifest, _, _, metadata = source
    inputs = ready(source)
    before = manifest.read_bytes()
    fake_compiler(monkeypatch, source, mode)
    with pytest.raises(subprocess.CalledProcessError if mode == "failed" else RuntimeError, match=message):
        build_installer.build(inputs, root / "installers", update_source=True)
    if mode == "changed-manifest":
        assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == "0.2.2"
    else:
        assert manifest.read_bytes() == before
    assert json.loads(manifest.read_text(encoding="utf-8"))["sha256"] == metadata["sha256"]


@pytest.mark.parametrize("version", ['0.2.1\"\n[Run]', "v0.2.1", "0.2", "0.2.1-beta", "99999.0.0", None])
def test_invalid_metadata_versions_are_rejected(source, version):
    source[1].write_text(json.dumps(source[4] | {"version": version}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="version"):
        ready(source)


def test_relative_metadata_paths_resolve_from_manifest(source):
    metadata = source[4] | {"bundle": "KillZHS", "archive": Path(source[4]["archive"]).name}
    source[1].write_text(json.dumps(metadata), encoding="utf-8")
    assert ready(source).bundle == source[2]


@pytest.mark.parametrize("value", ["not-json", "[]", "null"])
def test_invalid_manifest_fails_early(source, value):
    source[1].write_text(value, encoding="utf-8")
    with pytest.raises(RuntimeError, match="metadata"):
        ready(source)


@pytest.mark.parametrize("corruption, message", [
    ("exe", "AMD64"), ("runtime", "onedir"), ("zip", "checksum"),
    ("compiler", "ISCC.exe"), ("language", "ChineseSimplified"),
])
def test_missing_or_tampered_build_inputs_fail_without_download(source, corruption, message):
    _, _, bundle, compiler, metadata = source
    if corruption == "exe":
        (bundle / "KillZHS.exe").write_bytes(b"not PE")
    elif corruption == "runtime":
        (bundle / "_internal" / "python314.dll").unlink()
    elif corruption == "zip":
        Path(metadata["archive"]).write_bytes(b"changed")
    elif corruption == "compiler":
        compiler.unlink()
    else:
        (compiler.parent / "Languages" / "ChineseSimplified.isl").write_bytes(b"unverified")
    with pytest.raises(RuntimeError, match=message):
        ready(source)


def test_rejects_explicit_bundle_mismatch(source):
    with pytest.raises(RuntimeError, match="must match"):
        build_installer.preflight(source[1], source[0], source[3], root=source[0])


@pytest.mark.parametrize("path", ["a*b", "a?b", "{app}", 'quoted"name', "new\nline"])
def test_inno_path_injection_is_rejected(tmp_path, path):
    with pytest.raises(RuntimeError, match="Unsafe"):
        build_installer.safe_path(tmp_path / path)


def test_input_links_and_runtime_userdata_are_rejected(source, monkeypatch):
    link = source[2] / "_internal"
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == link or original(path))
    with pytest.raises(RuntimeError, match="Links"):
        ready(source)
    monkeypatch.setattr(Path, "is_junction", original)
    (source[2] / "cookies.json").write_text("private fixture", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Private/runtime"):
        ready(source)


def test_output_inside_bundle_rejected_before_creation(source, monkeypatch):
    fake_compiler(monkeypatch, source)
    with pytest.raises(RuntimeError, match="outside"):
        build_installer.build(ready(source), source[2] / "installers")
    assert not (source[2] / "installers").exists()


def test_template_installs_per_user_offers_tasks_and_preserves_userdata():
    template = (Path(__file__).resolve().parents[1] / "packaging" / "windows.iss").read_text(encoding="utf-8")
    sections = {}
    section = None
    for line in template.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            sections[section] = []
        elif section and line and not line.startswith(";"):
            sections[section].append(line)
    setup = dict(line.split("=", 1) for line in sections["Setup"])
    assert setup["DefaultDirName"] == r"{localappdata}\Programs\KillZHS"
    assert setup["PrivilegesRequired"] == "lowest"
    assert setup["DisableDirPage"] == "no"
    assert setup["MinVersion"] == "10.0"
    assert setup["ArchitecturesAllowed"] == "x64os"
    assert setup["CloseApplications"] == "yes" and setup["RestartApplications"] == "no"
    assert "AppMutex" not in setup  # The app does not implement a Windows mutex.
    assert setup["UsePreviousTasks"] == "no"
    assert 'Name: "desktopicon";' in sections["Tasks"][0]
    assert "Flags: unchecked" in sections["Tasks"][0]
    desktop = next(line for line in sections["Icons"] if "{userdesktop}" in line)
    assert "Tasks: desktopicon" in desktop
    assert any("{userprograms}" in line and "KillZHS.exe" in line for line in sections["Icons"])
    assert any("{uninstallexe}" in line for line in sections["Icons"])
    assert "postinstall skipifsilent unchecked" in sections["Run"][0]
    assert "ChineseLanguageFile" in sections["Languages"][0]
    assert not {"Code", "InstallDelete", "UninstallDelete", "Registry"} & sections.keys()
    assert "{localappdata}\\KillZHS" not in template
    assert "taskkill" not in template.lower()


def test_ci_release_artifacts_exclude_local_receipts_and_compiler_is_pinned():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-windows.yml").read_text(
        encoding="utf-8"
    )
    final_upload = workflow.rsplit("- uses: actions/upload-artifact@v4", 1)[1]
    assert workflow.count("- uses: actions/upload-artifact@v4") == 1
    paths = final_upload.split("path: |", 1)[1].splitlines()
    assert {path.strip() for path in paths if path.strip()} == {
        "output/release/**/*.zip", "output/release/**/SHA256SUMS.txt",
        "output/installers/**/*.exe", "output/installers/**/SHA256SUMS.txt",
    }
    assert "INNO_SETUP_VERSION: '6.7.3'" in workflow
    assert "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732" in workflow
    assert build_installer.CHINESE_SHA256 in workflow
    assert "/PORTABLE=1" in workflow and "Get-AuthenticodeSignature" in workflow
    assert "winget" not in workflow
