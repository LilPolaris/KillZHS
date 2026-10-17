"""Assessment protocol, complete-sheet submission, interruption and UI contracts."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from killzhs_assessments import (AssessmentError, SharedAssessments, item_key,
                                parse_answer, questions_from_paper)
from killzhs_config import AI_DEFAULTS, InputError
from killzhs_runtime import TaskControl, TaskStopped
from webui import Controller, course_key


def paper():
    return {"problemNum": 2, "workExamParts": [{"questionDtos": [
        {"eid": "q1", "name": "<p>1 + 1 = ?</p>", "questionType": {"id": 1},
         "questionOptions": [{"id": "a", "content": "2"}, {"id": "b", "content": "3"}]},
        {"eid": "q2", "name": "选择偶数", "questionType": {"id": 2},
         "questionOptions": [{"id": "c", "content": "2"}, {"id": "d", "content": "4"}]}
    ]}]}


class PaperTest(unittest.TestCase):
    def test_structured_choice_ids_are_validated_without_guessing(self):
        questions = questions_from_paper(paper())
        self.assertEqual(questions[0]["text"], "1 + 1 = ?")
        self.assertEqual(parse_answer('```json\n{"option_ids":["a"]}\n```', questions[0]), ["a"])
        self.assertEqual(parse_answer('{"option_ids":["d","c"]}', questions[1]), ["c", "d"])
        for answer in ('{"option_ids":["x"]}', '{"option_ids":[]}', '{"option_ids":["a","b"]}',
                       '{"option_ids":["a","a"]}', '{"option_ids":["a"],"other":"bad"}', 'A'):
            with self.subTest(answer=answer), self.assertRaises(AssessmentError):
                parse_answer(answer, questions[0])

    def test_unsupported_or_incomplete_papers_do_not_become_blank_answers(self):
        for change in (lambda p: p.update(problemNum=3),
                       lambda p: p["workExamParts"][0]["questionDtos"][0].update(questionType=3),
                       lambda p: p["workExamParts"][0]["questionDtos"][0].update(name='<img src="x">'),
                       lambda p: p["workExamParts"][0]["questionDtos"][0].update(name='\ue000'),
                       lambda p: p["workExamParts"][0]["questionDtos"][0].update(questionDatas=[{"suffix": "mp3"}])):
            value = paper()
            change(value)
            with self.assertRaises(AssessmentError):
                questions_from_paper(value)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events, self.calls = [], []
        self.service = SharedAssessments(Mock(), TaskControl(), copy.deepcopy(AI_DEFAULTS),
            self.temp.name, emit=lambda name, **value: self.events.append((name, value)), log=Mock())
        self.service.recruit, self.service.school, self.service.course_id = "r1", "s1", "c1"
        self.row = {"id": "student1", "exam_id": "exam1", "kind": "chapter", "course_key": "zhidao:course1",
                    "name": "Chapter test", "state": 1, "start": "", "end": ""}
        self.service.answer = Mock(side_effect=[["a"], ["c", "d"]])
        self.service.query = self.query

    def tearDown(self):
        self.temp.cleanup()

    def query(self, path, data, **kwargs):
        self.calls.append((path, data, kwargs))
        if path == "answer/getSaveAnswerLockResult":
            return {"state": 0}
        if path in ("student/doHomework", "student/doExam"):
            return {"state": 1, "examBase": paper()}
        if path == "answer/saveStudentAnswer":
            return {"statu": 1}
        if path in ("answer/getStuAnswerInfo", "answer/getStuAnswerInfoNew"):
            return {"q1": {"answer": "a"}, "q2": {"answer": "d,c"}}
        if path == "answer/submit":
            return {"statu": 0, "score": 100}
        raise AssertionError(path)

    def test_full_paper_is_generated_saved_read_back_and_submitted_once(self):
        result = self.service.run(self.row)
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(result["score"], 100)
        paths = [call[0] for call in self.calls]
        self.assertEqual(paths, ["student/doHomework", "answer/saveStudentAnswer", "answer/saveStudentAnswer",
                                 "answer/getStuAnswerInfoNew", "answer/submit"])
        self.assertEqual(self.calls[-1][1]["achieveCount"], 2)
        answer = json.loads(self.calls[2][1]["stuExamAnswer"])[0]
        self.assertEqual((answer["eid"], answer["answer"], answer["questionType"]), ("q2", "c,d", 2))
        with self.assertRaisesRegex(AssessmentError, "不会重复"):
            self.service.run(self.row)
        self.assertEqual(sum(path == "answer/submit" for path, *_ in self.calls), 1)

    def test_regular_exam_uses_its_host_and_a_lock_check(self):
        self.row["kind"] = "exam"
        self.service.run(self.row)
        self.assertEqual(self.calls[0][0], "answer/getSaveAnswerLockResult")
        self.assertEqual(self.calls[1][0], "student/doExam")
        self.assertEqual(self.calls[1][2]["kind"], "exam")
        self.assertEqual(self.calls[-1][2]["kind"], "exam")
        self.assertEqual(self.calls[2][2], {})  # same answer-save host as the official form

    def test_generation_failure_writes_no_answers_or_submission(self):
        self.service.answer = Mock(side_effect=[["a"], AssessmentError("invalid answer")])
        with self.assertRaises(AssessmentError):
            self.service.run(self.row)
        self.assertEqual([x[0] for x in self.calls], ["student/doHomework"])

    def test_budget_and_unsupported_paper_stop_before_generation(self):
        self.service.remaining_requests = lambda: 1
        with self.assertRaisesRegex(AssessmentError, "上限不足"):
            self.service.run(self.row)
        self.service.answer.assert_not_called()
        self.assertEqual(len(self.calls), 1)

    def test_save_refusal_and_readback_mismatch_never_submit(self):
        for failure in ("save", "readback"):
            with self.subTest(failure=failure):
                self.calls.clear()
                self.service.answer = Mock(side_effect=[["a"], ["c", "d"]])
                def query(path, data, **kwargs):
                    if failure == "save" and path == "answer/saveStudentAnswer":
                        self.calls.append((path, data, kwargs))
                        return {"statu": -10}
                    if failure == "readback" and path == "answer/getStuAnswerInfoNew":
                        return {"q1": {"answer": "b"}, "q2": {"answer": "c,d"}}
                    return self.query(path, data, **kwargs)
                self.service.query = query
                with self.assertRaises(AssessmentError) as caught:
                    self.service.run(self.row)
                self.assertNotIn("answer/submit", [x[0] for x in self.calls])
                if failure == "save":
                    self.assertEqual(caught.exception.reason, "verification")

    def test_unknown_submission_and_process_restart_do_not_repeat_submit(self):
        def query(path, data, **kwargs):
            if path == "answer/submit":
                self.calls.append((path, data, kwargs))
                raise TimeoutError("reply lost")
            return self.query(path, data, **kwargs)
        self.service.query = query
        with self.assertRaises(TimeoutError):
            self.service.run(self.row)
        restarted = SharedAssessments(Mock(), TaskControl(), AI_DEFAULTS, self.temp.name, emit=Mock(), log=Mock())
        with self.assertRaisesRegex(AssessmentError, "待确认"):
            restarted.run(self.row)
        self.assertEqual(sum(path == "answer/submit" for path, *_ in self.calls), 1)

    def test_stop_before_submission_preserves_answers_without_submitting(self):
        def query(path, data, **kwargs):
            result = self.query(path, data, **kwargs)
            if path == "answer/getStuAnswerInfoNew":
                self.service.control.check = Mock(side_effect=TaskStopped("stopped"))
            return result
        self.service.query = query
        with self.assertRaises(TaskStopped):
            self.service.run(self.row)
        self.assertNotIn("answer/submit", [x[0] for x in self.calls])
        self.assertEqual(len(list((Path(self.temp.name) / "assessment-attempts").glob("*.json"))), 1)

    def test_scan_never_opens_a_paper(self):
        self.service.course_key = "zhidao:course1"
        self.service.authenticated.add("exam")
        entry = {"id": "s1", "examId": "e1", "state": 1, "examName": "Test"}
        calls = []
        def query(path, data, **kwargs):
            calls.append(path)
            if path == "student/getStudentHomework":
                return {"studentHomeworkList": [entry], "next": -1}
            return [entry]
        self.service.query = query
        rows, incomplete = self.service.scan()
        self.assertEqual(len(rows), 2)
        self.assertFalse(incomplete)
        self.assertTrue(rows[0]["url"].startswith("https://onlineexamh5new.zhihuishu.com/"))
        self.assertEqual(calls, ["student/getStudentHomework", "student/getStudentFinalExam"])

    def test_exam_sso_uses_cas_and_expired_credentials_request_a_fresh_scan_login(self):
        self.service.session = Mock()
        response = Mock(url="https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1/student/getStudentHomework",
                        status_code=404, history=[Mock(status_code=302)])
        self.service.session.get.return_value = response
        self.service.authenticate("chapter")
        call = self.service.session.get.call_args
        self.assertEqual(call.args[0], "https://passport.zhihuishu.com/cas/login")
        self.assertTrue(call.kwargs["params"]["service"].endswith("/student/getStudentHomework"))
        response.raise_for_status.assert_not_called()
        response.url = "https://passport.zhihuishu.com/login"
        with self.assertRaises(AssessmentError) as caught:
            self.service.authenticate("exam")
        self.assertEqual(caught.exception.reason, "auth")
        self.assertIn("重新扫码登录工作台", str(caught.exception))

    def test_exam_transport_failure_preserves_chapter_list(self):
        import requests
        self.service.course_key = "zhidao:course1"
        self.service.authenticated.add("exam")
        def query(path, data, **kwargs):
            if path == "student/getStudentHomework":
                return {"studentHomeworkList": [{"id": "s1", "examId": "e1", "state": 1, "examName": "Chapter"}], "next": -1}
            raise requests.HTTPError("HTTP 404")
        self.service.query = query
        rows, incomplete = self.service.scan()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "chapter")
        self.assertEqual(incomplete, ["exam"])

    def test_transport_encrypts_unicode_and_has_no_redirect_or_retry(self):
        import requests
        from zd_utils import Cipher, EXAM_KEY
        self.service.session = requests.Session()
        reply = Mock(status_code=200)
        reply.json.return_value = {"status": "200", "rt": {"statu": 1}}
        with patch.object(self.service.session, "post", return_value=reply) as post:
            SharedAssessments.query(self.service, "answer/saveStudentAnswer", {"answer": "中文"})
            self.assertEqual(post.call_count, 1)
            kwargs = post.call_args.kwargs
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(json.loads(Cipher(EXAM_KEY).decrypt(kwargs["data"]["secretStr"])), {"answer": "中文"})
            self.assertEqual(self.service.session.get_adapter("https://").max_retries.total, 0)
        reply.json.return_value = {"status": 200, "code": -12}
        with patch.object(self.service.session, "post", return_value=reply), self.assertRaises(AssessmentError) as caught:
            SharedAssessments.query(self.service, "answer/submit", {})
        self.assertEqual(caught.exception.reason, "verification")
        self.service.session.close()


class ControllerTest(unittest.TestCase):
    def test_failed_task_cannot_leave_a_paper_marked_running(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory, demo=True)
            try:
                key = course_key(controller.courses[0])
                controller.dispatch({"action": "assessment_scan", "courses": [key]})
                controller.thread.join(3)
                selected = item_key(controller.assessments[0])
                controller.dispatch({"action": "ai_settings", "ai": {"enabled": True}})
                def failed_demo(payload):
                    controller.event({"type": "assessment_result", "key": selected, "state": "running"})
                    controller.event({"type": "result", "message": "模拟网络错误，结果需核对。", "error": True})
                controller.run_demo = failed_demo
                controller.dispatch({"action": "assessment_run", "courses": [key], "assessments": [selected]})
                controller.thread.join(3)
                self.assertEqual(controller.assessment_results[selected]["state"], "attention")
                self.assertIn("核对", controller.assessment_results[selected]["message"])
            finally:
                controller.close()

    def test_exam_verification_keeps_its_task_kind_and_completed_video_course(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory, demo=True)
            try:
                course = controller.courses[0]
                course["status"] = "done"  # Video completion does not finish an exam.
                controller.action = "assessment_run"
                controller.job_courses = [course]
                controller.current = course_key(course)
                controller.event({"type": "result", "reason": "verification", "error": True,
                                  "message": "平台要求网页验证。"})
                self.assertEqual(controller.verification["action"], "assessment_run")
                self.assertEqual(controller.verification["course_keys"], [course_key(course)])
                self.assertEqual(course["status"], "done")
            finally:
                controller.close()

    def test_assessment_actions_use_scanned_items_and_require_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory, demo=True)
            try:
                key = course_key(controller.courses[0])
                controller.dispatch({"action": "assessment_scan", "courses": [key]})
                controller.thread.join(3)
                self.assertEqual(len(controller.assessments), 2)
                keys = [item_key(row) for row in controller.assessments]
                with self.assertRaisesRegex(InputError, "启用 AI"):
                    controller.dispatch({"action": "assessment_run", "courses": [key], "assessments": keys})
                controller.dispatch({"action": "ai_settings", "ai": {"enabled": True}})
                with self.assertRaisesRegex(InputError, "清单已变化"):
                    controller.dispatch({"action": "assessment_run", "courses": [key], "assessments": ["invented"]})
                controller.dispatch({"action": "assessment_run", "courses": [key], "assessments": keys})
                controller.thread.join(3)
                self.assertEqual(len(controller.assessment_results), 2)
                self.assertTrue(all(value["state"] == "submitted" for value in controller.assessment_results.values()))
                self.assertEqual(controller.ai_requests, 0)
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
