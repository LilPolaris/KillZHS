"""Failed saves can be diagnosed and retried without paying to regenerate answers."""

import copy
import json
from unittest.mock import Mock

import pytest

from killzhs.ai.providers import RequestBudget
from killzhs.platform.assessments import AssessmentError, SharedAssessments
from killzhs.runtime import TaskControl, TaskStopped
from tests.protocol.scenarios import AI_BASE
from tests.test_platform_assessments import paper


@pytest.fixture
def workflow(tmp_path):
    row = {"id": "student1", "exam_id": "exam1", "kind": "chapter", "course_key": "zhidao:course1",
           "name": "测试", "state": 1, "start": "", "end": ""}
    options = {"save": {"statu": 1}, "submit": {"statu": 0}, "paper": paper(),
               "readback": {"q1": {"answer": "a"}, "q2": {"answer": "d,c"}}}
    calls = []

    def query(path, data, **kwargs):
        calls.append((path, data))
        if path == "student/doHomework":
            return {"state": 1, "examBase": copy.deepcopy(options["paper"])}
        if path == "answer/saveStudentAnswer":
            return options["save"]
        if path == "answer/getStuAnswerInfoNew":
            return options["readback"]
        if path == "answer/submit":
            return options["submit"]
        raise AssertionError(path)

    def service():
        instance = SharedAssessments(Mock(), TaskControl(), copy.deepcopy(AI_BASE), tmp_path,
                                     budget=RequestBudget(10), emit=Mock(), log=Mock())
        instance.recruit, instance.school, instance.course_id = "r1", "s1", "c1"
        instance.query = query
        instance.answer = Mock(side_effect=[["a"], ["c", "d"]])
        return instance

    return row, options, calls, service, tmp_path


def journal(tmp_path):
    path, = (tmp_path / "assessment-attempts").glob("*.json")
    return path, json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("saved", [1, "1"])
@pytest.mark.parametrize("submitted", [0, 1, "0", "1"])
def test_integer_and_string_success_codes_require_readback_before_submission(workflow, saved, submitted):
    row, options, calls, service, _ = workflow
    options.update(save={"statu": saved}, submit={"statu": submitted})
    result = service().run(row)
    assert result["state"] == "submitted"
    assert ("等待平台批阅" in result["message"]) is (submitted in (1, "1"))
    assert [path for path, _ in calls][-2:] == ["answer/getStuAnswerInfoNew", "answer/submit"]


@pytest.mark.parametrize("response,reason,detail", [
    ({"statu": -10}, "verification", "答题验证"),
    ({"statu": "-10"}, "verification", "答题验证"),
    ({"statu": -20}, None, "-20"),
    ({"statu": "-20"}, None, "-20"),
    ({"statu": 0}, None, "返回码：0"),
    ({"statu": -3}, None, "返回码：-3"),
    ({"statu": True}, None, "缺失或格式不支持"),
    ({"statu": 1.0}, None, "缺失或格式不支持"),
    ({"statu": "1.0"}, None, "缺失或格式不支持"),
    ({"statu": "secret-token"}, None, "缺失或格式不支持"),
    ({"statu": " 1"}, None, "缺失或格式不支持"),
    ({}, None, "缺失或格式不支持"),
    (None, None, "缺失或格式不支持"),
    ([{"statu": 1}], None, "缺失或格式不支持"),
])
def test_save_refusal_is_specific_keeps_answers_and_never_retries_or_submits(workflow, response, reason, detail):
    row, options, calls, service, tmp_path = workflow
    options["save"] = response
    with pytest.raises(AssessmentError, match=detail) as caught:
        service().run(row)
    assert caught.value.reason == reason
    assert "secret-token" not in str(caught.value)
    assert [path for path, _ in calls] == ["student/doHomework", "answer/saveStudentAnswer"]
    _, saved = journal(tmp_path)
    assert saved["state"] == "answers_ready"
    assert len(saved["answers"]) == 2
    assert saved["save_failure"]["question_number"] == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_retry_reuses_validated_answers_after_process_restart_with_zero_budget(workflow, legacy):
    row, options, calls, service, tmp_path = workflow
    options["save"] = {"statu": -20}
    with pytest.raises(AssessmentError):
        service().run(row)
    if legacy:
        path, saved = journal(tmp_path)
        saved.pop("questions_hash")
        saved.pop("save_failure")
        path.write_text(json.dumps(saved), encoding="utf-8")
    calls.clear()
    options["save"] = {"statu": "1"}
    restarted = service()
    restarted.budget = RequestBudget(0)
    restarted.answer = Mock(side_effect=AssertionError("Cached retry must not call a model"))
    assert restarted.run(row)["state"] == "submitted"
    restarted.answer.assert_not_called()
    assert restarted.budget.used == 0
    assert sum(path == "answer/submit" for path, _ in calls) == 1
    assert [json.loads(data["stuExamAnswer"])[0]["answer"] for path, data in calls
            if path == "answer/saveStudentAnswer"] == ["a", "c,d"]
    assert "不再请求模型" in restarted.log.call_args_list[0].args[0]
    with pytest.raises(AssessmentError, match="不会重复"):
        service().run(row)
    assert sum(path == "answer/submit" for path, _ in calls) == 1


@pytest.mark.parametrize("change", [
    lambda d: d["answers"].pop(),
    lambda d: d["answers"].reverse() or d["answers"][0].update(eid="unknown-question"),
    lambda d: d["answers"][0].update(eid="q2"),
    lambda d: d["answers"][0].update(examId="other-exam"),
    lambda d: d["answers"][0].update(stuExamId="other-student"),
    lambda d: d["answers"][0].update(recruitId="other-course"),
    lambda d: d["answers"][0].update(schoolId="other-school"),
    lambda d: d["answers"][0].update(questionType=2),
    lambda d: d["answers"][0].update(questionType=True),
    lambda d: d["answers"][0].update(answer="unknown-option"),
    lambda d: d["answers"][0].update(answer="a,b"),
    lambda d: d["answers"][0].update(answer="a,a"),
    lambda d: d.update(questions_hash="other-paper"),
])
def test_cache_mismatch_stops_before_model_or_answer_writes(workflow, change):
    row, options, calls, service, tmp_path = workflow
    options["save"] = {"statu": 0}
    with pytest.raises(AssessmentError):
        service().run(row)
    path, saved = journal(tmp_path)
    change(saved)
    path.write_text(json.dumps(saved), encoding="utf-8")
    original = path.read_bytes()
    calls.clear()
    restarted = service()
    with pytest.raises(AssessmentError, match="与当前试卷不一致"):
        restarted.run(row)
    restarted.answer.assert_not_called()
    assert [p for p, _ in calls] == ["student/doHomework"]
    assert path.read_bytes() == original


def test_same_ids_with_changed_question_text_reject_new_cache(workflow):
    row, options, calls, service, _ = workflow
    options["save"] = {"statu": 0}
    with pytest.raises(AssessmentError):
        service().run(row)
    options["paper"]["workExamParts"][0]["questionDtos"][0]["name"] = "Changed question"
    calls.clear()
    restarted = service()
    with pytest.raises(AssessmentError, match="与当前试卷不一致"):
        restarted.run(row)
    restarted.answer.assert_not_called()
    assert [p for p, _ in calls] == ["student/doHomework"]


def test_reused_answers_still_require_matching_platform_readback(workflow):
    row, options, calls, service, _ = workflow
    options["save"] = {"statu": 0}
    with pytest.raises(AssessmentError):
        service().run(row)
    options.update(save={"statu": 1}, readback={"q1": {"answer": "b"}, "q2": {"answer": "c,d"}})
    calls.clear()
    restarted = service()
    with pytest.raises(AssessmentError, match="复核不一致"):
        restarted.run(row)
    restarted.answer.assert_not_called()
    assert not any(p == "answer/submit" for p, _ in calls)


@pytest.mark.parametrize("reply", [TimeoutError("Lost reply"), {"state": 4}, {"state": 1, "examBase": {}}])
def test_reopening_failure_keeps_recoverable_answers_unchanged(workflow, reply):
    row, options, calls, service, tmp_path = workflow
    options["save"] = {"statu": 0}
    with pytest.raises(AssessmentError):
        service().run(row)
    path, _ = journal(tmp_path)
    original = path.read_bytes()
    calls.clear()
    restarted = service()
    restarted.query = Mock(side_effect=reply) if isinstance(reply, Exception) else Mock(return_value=reply)
    with pytest.raises((TimeoutError, AssessmentError)):
        restarted.run(row)
    restarted.answer.assert_not_called()
    assert restarted.query.call_count == 1
    assert path.read_bytes() == original


def test_stop_while_validating_reused_sheet_keeps_original_answers(workflow):
    row, options, calls, service, tmp_path = workflow
    options["save"] = {"statu": 0}
    with pytest.raises(AssessmentError):
        service().run(row)
    path, _ = journal(tmp_path)
    original = path.read_bytes()
    calls.clear()
    restarted = service()
    restarted.control.check = Mock(side_effect=[None, None, TaskStopped("stopped")])
    with pytest.raises(TaskStopped):
        restarted.run(row)
    restarted.answer.assert_not_called()
    assert [p for p, _ in calls] == ["student/doHomework"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("content", ["{broken", "[]"])
def test_damaged_journal_is_not_overwritten_or_used_to_repeat_submission(workflow, content):
    row, options, calls, service, tmp_path = workflow
    service().run(row)
    path, _ = journal(tmp_path)
    path.write_text(content, encoding="utf-8")
    calls.clear()
    restarted = service()
    with pytest.raises(AssessmentError, match="本地答题记录无法读取"):
        restarted.run(row)
    assert not calls
    assert path.read_text(encoding="utf-8") == content
    restarted.answer.assert_not_called()
