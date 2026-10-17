import copy
import tempfile
import unittest

from webui import Controller, InputError, atomic_json, course_key, normalize_course


class VerificationTest(unittest.TestCase):
    def test_verification_targets_current_course_and_excludes_completed_courses(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory, demo=True)
            try:
                first, current, pending = controller.courses[:3]
                first["status"] = "done"
                current["status"] = "attention"
                controller.job_courses = copy.deepcopy([first, current, pending])
                controller.current = course_key(current)
                controller.task_id = "a" * 32
                controller.event({"type": "result", "reason": "verification", "error": True,
                                  "message": "需要手动验证，本批次已停止。"})
                verification = controller.state()["verification"]
                self.assertEqual(verification["course_keys"], [course_key(current), course_key(pending)])
                self.assertEqual(verification["course_name"], current["name"])
                self.assertEqual(verification["url"],
                    "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=demo02")
                verification["course_keys"].clear()
                self.assertEqual(len(controller.verification["course_keys"]), 2)
            finally:
                controller.close()

    def test_verification_survives_restart_without_trusting_stored_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory)
            try:
                course = normalize_course({"id": "shared_course=", "kind": "zhidao", "name": "课程"})
                controller.courses = [course]
                controller.persist_courses()
                controller.job_courses = [course]
                controller.current = course_key(course)
                controller.action = "start"
                controller.started_at, controller.ended_at = 1, 2
                controller.task_id = "b" * 32
                controller.event({"type": "result", "reason": "verification", "error": True,
                                  "message": "需要手动验证。"})
                controller.status = "error"
                controller.verification["url"] = "javascript:invalid"
                controller.record_history()
            finally:
                controller.close()
            restored = Controller(directory)
            try:
                self.assertEqual(restored.verification["course_keys"], ["zhidao:shared_course="])
                self.assertEqual(restored.verification["url"],
                    "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=shared_course%3D")
            finally:
                restored.close()

    def test_legacy_history_restores_only_unique_course_names(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory)
            local = controller.local
            controller.close()
            atomic_json(local / "courses.json", [
                {"id": "one", "name": "重复名称", "kind": "zhidao"},
                {"id": "two", "name": "重复名称", "kind": "zhidao"}])
            atomic_json(local / "history.json", [{"action": "start", "status": "error",
                "message": "需要手动验证，本批次已停止。", "courses": ["重复名称"]}])
            restored = Controller(directory)
            try:
                self.assertFalse(restored.verification["direct"])
                self.assertEqual(restored.verification["url"], "https://www.zhihuishu.com/")
                self.assertEqual(restored.verification["course_keys"], [])
            finally:
                restored.close()

    def test_pending_verification_blocks_schedule_and_explicit_restart_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory, demo=True)
            try:
                key = course_key(controller.courses[0])
                controller.verification = controller.verification_context([key], key)
                with self.assertRaisesRegex(InputError, "仍需手动验证"):
                    controller.dispatch({"action": "start", "courses": [key]}, scheduled=True)
                controller.run_demo = lambda payload: controller.event(
                    {"type": "result", "message": "演示任务完成。", "error": False})
                controller.dispatch({"action": "start", "courses": [key]})
                controller.thread.join(2)
                self.assertIsNone(controller.verification)
                self.assertEqual([course_key(row) for row in controller.job_courses], [key])
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
