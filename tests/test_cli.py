"""Data-directory separation and actual operating-system instance locking."""

import os

import pytest

from killzhs.app.cli import ROOT, InstanceLock, default_data_directory


def test_data_directory_separates_new_and_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_data_directory(frozen=True) == tmp_path / "KillZHS" / "v2"
    assert default_data_directory(frozen=False) == ROOT / ".local" / "data"
    assert "demo" in default_data_directory(demo=True).name


@pytest.mark.skipif(os.name != "nt", reason="Windows locks reject a second descriptor in the same process")
def test_instance_lock_releases_for_restart(tmp_path):
    lock = InstanceLock(tmp_path)
    try:
        with pytest.raises(OSError):
            InstanceLock(tmp_path)
    finally:
        lock.close()
    InstanceLock(tmp_path).close()
