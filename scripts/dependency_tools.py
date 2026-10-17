"""Inspect installed dependency closures for supported Windows interpreters, offline."""

import os
import sys
from importlib import metadata, util
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def windows_environments():
    """Resolve markers separately for Windows x64 CPython 3.12 and 3.14."""
    return [
        {
            **default_environment(),
            "sys_platform": "win32",
            "os_name": "nt",
            "platform_system": "Windows",
            "platform_machine": "AMD64",
            "implementation_name": "cpython",
            "platform_python_implementation": "CPython",
            "python_version": version,
            "python_full_version": version + ".0",
            "implementation_version": version + ".0",
            "extra": "",
        }
        for version in ("3.12", "3.14")
    ]


def dependency_closure(requirements, lookup=metadata.distribution):
    """Validate every active edge, including repeated constraints and extras.

    Nothing is installed or fetched. The union includes dependencies needed by
    either supported interpreter, without mixing the two marker environments.
    """
    roots = [Requirement(value) for value in requirements]
    distributions = {}
    for environment in windows_environments():
        pending = [(requirement, frozenset()) for requirement in roots]
        visited = set()
        while pending:
            requirement, parent_extras = pending.pop()
            if requirement.marker and not any(
                requirement.marker.evaluate({**environment, "extra": extra})
                for extra in (parent_extras | {""})
            ):
                continue
            if requirement.url:
                raise RuntimeError(f"Direct URL dependencies cannot be verified offline: {requirement.name}")
            name = canonicalize_name(requirement.name)
            distribution = distributions.get(name)
            if distribution is None:
                distribution = lookup(name)
            # Check before the visited guard: a second edge may constrain a
            # previously visited distribution more strictly.
            if requirement.specifier and not requirement.specifier.contains(distribution.version, prereleases=True):
                raise RuntimeError(f"Installed dependency does not satisfy: {requirement}")
            distributions[name] = distribution
            key = (name, frozenset(requirement.extras))
            if key in visited:
                continue
            visited.add(key)
            pending.extend(
                (Requirement(value), frozenset(requirement.extras)) for value in distribution.requires or []
            )
    return distributions


def requirement_lines(path, *, _parents=()):
    """Read local requirements and recursive -r includes; reject hidden options."""
    path = Path(path).resolve()
    if path in _parents:
        raise ValueError(f"Recursive requirements include: {path.name}")
    requirements = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ", "--requirement=")):
            include = line.split("=", 1)[1] if line.startswith("--requirement=") else line.split(None, 1)[1]
            include = include.strip().strip("\"'")
            if "://" in include:
                raise ValueError("Only local requirements includes are supported")
            requirements.extend(requirement_lines(path.parent / include, _parents=(*_parents, path)))
        elif line.startswith("-"):
            raise ValueError(f"Unsupported requirements option in {path.name}: {line.split()[0]}")
        else:
            Requirement(line)  # Fail on malformed input instead of silently dropping it.
            requirements.append(line)
    return requirements


def validate_lock(path, lookup=metadata.distribution):
    """Check pins and ensure no transitive dependency is absent from the lock."""
    requirements = requirement_lines(path)
    names = set()
    for value in requirements:
        requirement = Requirement(value)
        pins = list(requirement.specifier)
        if requirement.url or len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise RuntimeError(f"Lock entries must use exact version pins: {requirement}")
        names.add(canonicalize_name(requirement.name))
    distributions = dependency_closure(requirements, lookup)
    missing = distributions.keys() - names
    if missing:
        raise RuntimeError("Lock is missing transitive dependencies: " + ", ".join(sorted(missing)))
    return distributions


def enable_build_tools(root=ROOT):
    """Use the active environment first, then existing local/legacy tools only.

    Disable bytecode writes before exposing the read-only legacy directory.
    There are deliberately no install, upgrade, or network fallbacks.
    """
    if util.find_spec("PyInstaller") is not None:
        return None
    root = Path(root)
    candidates = (root / ".local" / "build-tools", root.parent / "fuckZHS" / ".local" / "build-tools")
    for path in candidates:
        if (path / "PyInstaller" / "__init__.py").is_file():
            sys.dont_write_bytecode = True
            os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
            sys.path.append(str(path))
            return path
    raise RuntimeError("PyInstaller is unavailable; provide existing local build tools. No installation attempted.")
