"""Compile an existing Windows onedir bundle with an already-provisioned ISCC.

Offline and non-publishing. --check is read-only, including release metadata.
Every build has its own output directory; --update-manifest is an explicit opt-in.
"""

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .build_windows import assert_public_tree
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from build_windows import assert_public_tree

ROOT = Path(__file__).resolve().parents[1]
CHINESE_SHA256 = "7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278"


@dataclass(frozen=True)
class Inputs:
    manifest: Path
    metadata: dict
    bundle: Path
    iscc: Path
    language: Path
    template: Path
    payload: dict

    @property
    def version(self):
        return self.metadata["version"]


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def safe_path(path):
    """Reject links before resolve and characters that could widen Inno inputs."""
    path = Path(os.path.abspath(path))
    if any(char in str(path) for char in '\r\n\x00"{}*?'):
        raise RuntimeError(f"Unsafe installer path: {path}")
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise RuntimeError(f"Links are not allowed in installer paths: {component}")
    return path.resolve()


def metadata_path(value, manifest):
    if not isinstance(value, str) or not value:
        raise RuntimeError("Build metadata must include nonempty bundle and archive paths")
    path = Path(value)
    return safe_path(path if path.is_absolute() else manifest.parent / path)


def payload_hashes(bundle):
    assert_public_tree(bundle)
    return {
        path.relative_to(bundle).as_posix(): sha256(path)
        for path in sorted(bundle.rglob("*")) if path.is_file()
    }


def assert_x64_executable(executable):
    """Reject a ZIP, text placeholder, x86, or ARM64 binary as the application."""
    try:
        with executable.open("rb") as source:
            if source.read(2) != b"MZ":
                raise ValueError
            source.seek(0x3C)
            pe_offset = struct.unpack("<I", source.read(4))[0]
            source.seek(pe_offset)
            if source.read(6) != b"PE\0\0\x64\x86":
                raise ValueError
    except (OSError, ValueError, struct.error) as error:
        raise RuntimeError("Bundle must contain an AMD64 Windows PE KillZHS.exe") from error


def preflight(manifest, bundle, iscc, language=None, root=ROOT):
    manifest = safe_path(manifest)
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Cannot read build metadata: {manifest}") from error
    if not isinstance(metadata, dict):
        raise RuntimeError("Build metadata must be a JSON object")
    version = metadata.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d{1,5}(?:\.\d{1,5}){2,3}", version):
        raise RuntimeError("Build metadata version must have three or four numeric components")
    if any(int(part) > 65535 for part in version.split(".")):
        raise RuntimeError("Build metadata version components must be <= 65535")
    recorded_bundle = metadata_path(metadata.get("bundle"), manifest)
    bundle = safe_path(bundle) if bundle is not None else recorded_bundle
    if bundle != recorded_bundle:
        raise RuntimeError("--bundle must match the bundle in build metadata")
    if not (bundle / "_internal").is_dir() or not any((bundle / "_internal").iterdir()):
        raise RuntimeError("Expected a complete onedir bundle with nonempty _internal")
    assert_x64_executable(bundle / "KillZHS.exe")
    payload = payload_hashes(bundle)
    archive = metadata_path(metadata.get("archive"), manifest)
    digest = metadata.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise RuntimeError("Build metadata must retain a valid ZIP sha256")
    if not archive.is_file() or sha256(archive) != digest.lower():
        raise RuntimeError("Base ZIP checksum mismatch or archive missing")
    iscc = safe_path(iscc)
    if iscc.name.lower() != "iscc.exe" or not iscc.is_file():
        raise RuntimeError("ISCC.exe not found; provision the official compiler separately; no download attempted")
    language = safe_path(language if language is not None else iscc.parent / "Languages" / "ChineseSimplified.isl")
    if not language.is_file() or sha256(language) != CHINESE_SHA256:
        raise RuntimeError("Missing or unverified ChineseSimplified.isl; see docs/INSTALLER.md; no download attempted")
    template = safe_path(root / "packaging" / "windows.iss")
    if not template.is_file():
        raise RuntimeError("Missing packaging/windows.iss")
    return Inputs(manifest, metadata, bundle, iscc, language, template, payload)


def compiler_arguments(inputs, output):
    return [
        str(inputs.iscc), "/Q", f"/O{safe_path(output)}",
        f"/DAppVersion={inputs.version}", f"/DBundleDir={inputs.bundle}",
        f"/DChineseLanguageFile={inputs.language}", str(inputs.template),
    ]


def write_json_atomic(path, value):
    """Replace one explicitly selected metadata file without partial JSON writes."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def update_manifest(path, expected, installer_metadata):
    """Preserve ZIP identity and unknown fields; refuse concurrent base changes."""
    path = safe_path(path)
    current = json.loads(path.read_text(encoding="utf-8-sig"))
    if current != expected:
        raise RuntimeError("Build metadata changed during compilation; source manifest left untouched")
    updated = {**current, **installer_metadata}
    write_json_atomic(path, updated)
    return updated


def build(inputs, output_root, update_source=False):
    if sys.platform != "win32":
        raise RuntimeError("Compile the installer on Windows")
    output_root = safe_path(output_root)
    # Staging inside the source would accidentally embed outputs or credentials.
    if output_root == inputs.bundle or inputs.bundle in output_root.parents:
        raise RuntimeError("Installer output must be outside the application bundle")
    output_root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=f"{inputs.version}-", dir=output_root))
    subprocess.run(compiler_arguments(inputs, output), check=True, shell=False)
    installer = output / f"KillZHS-{inputs.version}-windows-x64-setup.exe"
    if not installer.is_file() or installer.stat().st_size == 0:
        raise RuntimeError("ISCC succeeded without producing the expected installer")
    if payload_hashes(inputs.bundle) != inputs.payload:
        raise RuntimeError("Application bundle changed during compilation; source manifest left untouched")
    installer_hash = sha256(installer)
    checksums = output / "SHA256SUMS.txt"
    checksums.write_text(f"{installer_hash}  {installer.name}\n", encoding="utf-8")
    fields = {
        "installer": str(installer), "installer_sha256": installer_hash,
        "installer_checksums": str(checksums),
        "installer_build": {
            "source_manifest": str(inputs.manifest), "compiler_sha256": sha256(inputs.iscc),
            "template_sha256": sha256(inputs.template), "language_sha256": sha256(inputs.language),
            "payload_sha256": hashlib.sha256(
                json.dumps(inputs.payload, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        },
    }
    write_json_atomic(output / "build.json", {**inputs.metadata, **fields})
    if update_source:
        update_manifest(inputs.manifest, inputs.metadata, fields)
    print(f"Installer ready: {installer}")
    print(f"Installer metadata: {output / 'build.json'}")
    return output / "build.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iscc", type=Path, default=ROOT / ".local" / "tools" / "inno" / "ISCC.exe")
    parser.add_argument("--manifest", type=Path, help="Build metadata; defaults to latest-build.json")
    parser.add_argument("--bundle", type=Path, help="Onedir folder; defaults to metadata bundle; must match it")
    parser.add_argument("--language-file", type=Path, help="Verified upstream ChineseSimplified.isl")
    parser.add_argument("--output-root", type=Path, default=ROOT / "output" / "installers")
    parser.add_argument("--update-manifest", action="store_true", help="Explicitly add installer fields to source metadata")
    parser.add_argument("--check", action="store_true", help="Read-only validation; never invokes ISCC or writes outputs")
    args = parser.parse_args(argv)
    manifest = args.manifest
    if manifest is None:
        manifest = args.bundle.parent / "build.json" if args.bundle else ROOT / "output" / "release" / "latest-build.json"
    inputs = preflight(manifest, args.bundle, args.iscc, args.language_file, ROOT)
    output_root = safe_path(args.output_root)
    if output_root == inputs.bundle or inputs.bundle in output_root.parents:
        raise RuntimeError("Installer output must be outside the application bundle")
    if args.check:
        print(f"Ready: KillZHS {inputs.version}; verified onedir, ZIP, ISCC path and Chinese wizard inputs.")
        return 0
    build(inputs, output_root, args.update_manifest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Installer build failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
