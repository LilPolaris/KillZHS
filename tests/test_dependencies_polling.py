from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
tools = ROOT / ".local" / "build-tools"
if tools.is_dir():
    sys.path.insert(0, str(tools))
from dependency_tools import dependency_closure
from webui import Controller


class DependenciesAndPollingTest(unittest.TestCase):
    def test_dependency_closure_includes_transitives_and_honors_markers_and_extras(self):
        distributions = {
            "root": types.SimpleNamespace(version="1.0", requires=[
                "leaf>=2; sys_platform == 'win32'",
                "old; python_version < '3.13'",
                "mac-only; sys_platform == 'darwin'",
                "optional; extra == 'feature'"]),
            "leaf": types.SimpleNamespace(version="2.0", requires=["root==1.0"]),
            "old": types.SimpleNamespace(version="1.0", requires=[]),
            "optional": types.SimpleNamespace(version="1.0", requires=[]),
        }
        result = dependency_closure(["root[feature]==1.0"], lookup=distributions.__getitem__)
        self.assertEqual(set(result), {"root", "leaf", "old", "optional"})
        with self.assertRaises(RuntimeError):
            dependency_closure(["leaf>=3"], lookup=distributions.__getitem__)

    def test_incremental_logs_history_revision_and_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory)
            try:
                first = controller.state()
                controller.log("new log")
                delta = controller.state(first["log_cursor"], first["history_revision"])
                self.assertEqual([row["message"] for row in delta["logs"]], ["new log"])
                self.assertNotIn("history", delta)
                self.assertFalse(delta["logs_reset"])
                controller.save_history()
                changed = controller.state(delta["log_cursor"], first["history_revision"])
                self.assertIn("history", changed)
                self.assertEqual(changed["logs"], [])
                reset = controller.state(controller.sequence + 1000, first["history_revision"])
                self.assertTrue(reset["logs_reset"])
                self.assertEqual(reset["logs"], list(controller.logs))
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
