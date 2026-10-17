"""Resolve installed Windows dependencies, honoring markers and requested extras."""

from importlib import metadata
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def windows_environments():
    environments = []
    for version in ("3.12", "3.14"):
        environments.append({**default_environment(), "sys_platform": "win32", "os_name": "nt",
            "platform_system": "Windows", "platform_machine": "AMD64",
            "python_version": version, "python_full_version": version + ".0", "extra": ""})
    return environments


def dependency_closure(requirements, lookup=metadata.distribution):
    pending = [(Requirement(value), frozenset()) for value in requirements]
    visited, distributions = set(), {}
    environments = windows_environments()
    while pending:
        requirement, parent_extras = pending.pop()
        if requirement.marker and not any(requirement.marker.evaluate({**env, "extra": extra})
                for env in environments for extra in (parent_extras or {""})):
            continue
        name = canonicalize_name(requirement.name)
        key = (name, frozenset(requirement.extras))
        if key in visited:
            continue
        visited.add(key)
        distribution = lookup(requirement.name)
        if requirement.specifier and distribution.version not in requirement.specifier:
            raise RuntimeError("Installed dependency does not satisfy: " + str(requirement))
        distributions[name] = distribution
        pending.extend((Requirement(value), frozenset(requirement.extras)) for value in distribution.requires or [])
    return distributions


def requirement_lines(path):
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "-"))]
