"""Grade refresh reads list fields without opening a paper or generating answers."""

from unittest.mock import Mock

import pytest

from killzhs.ai.providers import RequestBudget
from killzhs.platform.assessments import SharedAssessments, score_number
from killzhs.runtime import TaskControl


@pytest.mark.parametrize("raw,expected", [(0, 0), (100, 100), (87.5, 87.5), ("0", 0),
                                          ("100.0", 100), (" 87.5 ", 87.5)])
def test_numeric_scores_preserve_zero_and_fractional_marks(raw, expected):
    assert score_number(raw) == expected


@pytest.mark.parametrize("raw", [None, True, False, "", " ", -1, "-1", "待批阅", "<script>0</script>",
                                 float("nan"), float("inf"), "NaN", "Infinity", "1_0", "0x10", "100%", {}, [], 10 ** 400])
def test_missing_sentinel_or_invalid_scores_are_not_zero(raw):
    assert score_number(raw) is None


@pytest.fixture
def service(tmp_path):
    instance = SharedAssessments(Mock(), TaskControl(), {}, tmp_path,
                                 budget=RequestBudget(0), emit=Mock(), log=Mock())
    instance.course_key, instance.course_id = "zhidao:course1", "c1"
    instance.recruit, instance.school = "r1", "s1"
    return instance


@pytest.mark.parametrize("kind", ["chapter", "exam"])
def test_published_list_scores_include_total_and_percentage_without_unit_conversion(service, kind):
    result = service.normalize({"id": "s1", "examId": "e1", "state": "4", "score": "70.5",
                                "totalScore": "80", "oneHundredPont": "88.125"}, kind)
    assert (result["score"], result["total_score"], result["score_percent"]) == (70.5, 80, 88.125)


@pytest.mark.parametrize("state", [1, 2, 3, 5])
def test_unanswered_or_pending_list_placeholders_are_not_published_grades(service, state):
    result = service.normalize({"id": "s1", "examId": "e1", "state": state, "score": 0,
                                "totalScore": 100, "oneHundredPont": 0}, "chapter")
    assert "score" not in result
    assert "score_percent" not in result
    assert result["total_score"] == 100


def test_published_zero_is_retained_but_missing_grade_is_not_fabricated(service):
    row = {"id": "s1", "examId": "e1", "state": 4}
    assert "score" not in service.normalize(row, "chapter")
    assert service.normalize({**row, "score": "0"}, "chapter")["score"] == 0
    malformed = service.normalize({**row, "score": -1, "totalScore": 0, "oneHundredPont": 101}, "chapter")
    assert not {"score", "total_score", "score_percent"} & malformed.keys()


def test_grade_refresh_uses_only_list_endpoints_and_no_ai_or_attempt_records(service):
    service.authenticated.add("exam")
    service.answer = Mock(side_effect=AssertionError("Grade refresh must not request a model"))
    chapter = {"id": "s1", "examId": "e1", "state": 4, "score": "0", "totalScore": 100}
    pending = {"id": "s2", "examId": "e2", "state": 3, "score": 0, "totalScore": 100}
    service.query = Mock(side_effect=[{"studentHomeworkList": [chapter], "next": -1}, [pending]])
    rows, incomplete = service.scan()
    assert not incomplete
    assert rows[0]["score"] == 0 and rows[0]["total_score"] == 100
    assert "score" not in rows[1] and rows[1]["state"] == 3
    assert [call.args[0] for call in service.query.call_args_list] == [
        "student/getStudentHomework", "student/getStudentFinalExam"]
    assert service.budget.used == 0
    service.answer.assert_not_called()
    assert not service.directory.exists()
