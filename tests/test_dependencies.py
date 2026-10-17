"""Dependency closure regressions ported from the legacy offline tests."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.dependency_tools import dependency_closure, requirement_lines, validate_lock, windows_environments
from scripts.lock_dependencies import lock_text


def distribution(version="1.0", requires=()):
    return SimpleNamespace(version=version, requires=requires)


def test_dependency_closure_includes_transitives_and_honors_markers_and_extras():
    distributions = {
        "root": distribution(requires=[
            "leaf>=2; sys_platform == 'win32'", "old; python_version < '3.13'",
            "mac-only; sys_platform == 'darwin'", "optional; extra == 'feature'",
        ]),
        "leaf": distribution("2.0", ["root==1.0"]),
        "old": distribution(),
        "optional": distribution(),
    }
    result = dependency_closure(["root[feature]==1.0"], lookup=distributions.__getitem__)
    assert set(result) == {"root", "leaf", "old", "optional"}
    with pytest.raises(RuntimeError, match="does not satisfy"):
        dependency_closure(["leaf>=3"], lookup=distributions.__getitem__)


def test_repeated_requirement_checks_each_constraint_before_visited_guard():
    distributions = {"leaf": distribution()}
    # The pending stack first visits the valid requirement; the other edge must
    # still be checked after that visit.
    with pytest.raises(RuntimeError, match="leaf>=2"):
        dependency_closure(["leaf>=2", "leaf==1.0"], lookup=distributions.__getitem__)


def test_marker_environments_do_not_cross_contaminate_transitives():
    distributions = {
        "old": distribution(requires=["impossible; python_version >= '3.14'"]),
        "new": distribution(requires=["impossible; python_version < '3.13'"]),
    }
    assert set(dependency_closure(
        ["old; python_version < '3.13'", "new; python_version >= '3.14'"],
        lookup=distributions.__getitem__,
    )) == {"old", "new"}


def test_new_extras_are_expanded_on_a_previously_visited_package():
    distributions = {
        "root": distribution(requires=["left; extra == 'a'", "right[inner]; extra == 'b'"]),
        "left": distribution(),
        "right": distribution(requires=["leaf; extra == 'inner'"]),
        "leaf": distribution(),
    }
    assert set(dependency_closure(["root[a]", "root[b]"], lookup=distributions.__getitem__)) == {
        "root", "left", "right", "leaf",
    }


def test_unrequested_extras_do_not_leak_to_child_packages():
    distributions = {
        "root": distribution(requires=["leaf; extra == 'feature'"]),
        "leaf": distribution(requires=["unexpected; extra == 'feature'"]),
    }
    assert set(dependency_closure(["root[feature]"], lookup=distributions.__getitem__)) == {"root", "leaf"}


def test_names_are_canonicalized():
    assert set(dependency_closure(["Some_Package.Name"], lookup=lambda name: distribution())) == {"some-package-name"}


def test_direct_urls_are_not_treated_as_verified_installed_dependencies():
    with pytest.raises(RuntimeError, match="cannot be verified offline"):
        dependency_closure(["thing @ https://example.invalid/thing.whl"], lookup=lambda name: distribution())


def test_windows_environments_cover_supported_cpython_versions():
    environments = windows_environments()
    assert {env["python_version"] for env in environments} == {"3.12", "3.14"}
    assert all(env["sys_platform"] == "win32" and env["platform_machine"] == "AMD64" for env in environments)
    assert all(env["implementation_version"] == env["python_full_version"] for env in environments)


def test_requirements_reader_expands_relative_includes_and_comments(tmp_path):
    nested = tmp_path / "locks"
    nested.mkdir()
    (nested / "runtime.txt").write_text("leaf==2.0 # pinned\n", encoding="utf-8")
    requirements = tmp_path / "build.txt"
    requirements.write_text('# comment\n-r locks/runtime.txt\nroot==1.0\n', encoding="utf-8-sig")
    assert requirement_lines(requirements) == ["leaf==2.0", "root==1.0"]


@pytest.mark.parametrize("line", ["-r loop.txt", "--requirement loop.txt", "--requirement=loop.txt"])
def test_recursive_requirements_includes_fail(tmp_path, line):
    path = tmp_path / "loop.txt"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(ValueError, match="Recursive"):
        requirement_lines(path)


@pytest.mark.parametrize("line", ["--index-url https://example.invalid", "-r https://example.invalid/lock.txt"])
def test_requirements_reader_rejects_options_and_remote_includes(tmp_path, line):
    path = tmp_path / "requirements.txt"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(ValueError):
        requirement_lines(path)


def test_lock_must_include_all_transitives(tmp_path):
    path = tmp_path / "lock.txt"
    path.write_text("root==1.0\n", encoding="utf-8")
    distributions = {"root": distribution(requires=["leaf"]), "leaf": distribution()}
    with pytest.raises(RuntimeError, match="missing transitive dependencies: leaf"):
        validate_lock(path, lookup=distributions.__getitem__)
    path.write_text("root==1.0\nleaf==1.0\n", encoding="utf-8")
    assert set(validate_lock(path, lookup=distributions.__getitem__)) == {"root", "leaf"}


@pytest.mark.parametrize("value", ["root>=1", "root==1.*", "root", "root @ https://example.invalid/root.whl"])
def test_locks_require_exact_pins(tmp_path, value):
    path = tmp_path / "lock.txt"
    path.write_text(value, encoding="utf-8")
    with pytest.raises(RuntimeError, match="exact version pins"):
        validate_lock(path, lookup=lambda name: distribution())


def test_lock_text_is_sorted_and_build_lock_includes_runtime():
    result = lock_text({"z": distribution("2.0"), "a": distribution()}, "-r requirements-lock.txt")
    assert result.splitlines()[1:] == ["-r requirements-lock.txt", "a==1.0", "z==2.0"]
    assert result.endswith("\n")


def test_project_runtime_lock_closure_matches_declared_dependencies():
    root = Path(__file__).resolve().parents[1]
    assert validate_lock(root / "requirements-lock.txt").keys() == dependency_closure(
        requirement_lines(root / "requirements.txt")
    ).keys()
