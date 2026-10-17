"""Opaque paper identifiers are JSON data, unlike list IDs used in URL paths."""

import copy
import json
from unittest.mock import Mock

import pytest

from killzhs.ai.providers import RequestBudget
from killzhs.platform.assessments import (
    AssessmentError,
    SharedAssessments,
    ident,
    parse_answer,
    questions_from_paper,
)
from killzhs.runtime import TaskControl
from tests.protocol.scenarios import AI_BASE
from tests.test_platform_assessments import paper


@pytest.mark.parametrize("location", ["question", "option"])
def test_encrypted_question_and_option_ids_are_preserved_verbatim(location):
    value = paper()
    question = value["workExamParts"][0]["questionDtos"][0]
    opaque = "aB+cD/ef0123456789=="
    if location == "question":
        question["eid"] = opaque
    else:
        question["questionOptions"][0]["id"] = opaque
    parsed = questions_from_paper(value)[0]
    assert (parsed["id"] if location == "question" else parsed["options"][0]["id"]) == opaque
    if location == "option":
        assert parse_answer(json.dumps({"option_ids": [opaque]}), parsed) == [opaque]


@pytest.mark.parametrize("kind", ["chapter", "exam"])
def test_opaque_ids_survive_generation_save_readback_and_single_submission(tmp_path, kind):
    original = paper()
    qids, options = ["question+one/==", "question-two_=="], ["option+A/==", "option+B/=="]
    for row, qid, option in zip(original["workExamParts"][0]["questionDtos"], qids, options, strict=True):
        row["eid"] = qid
        row["questionOptions"][0]["id"] = option
    calls = []

    def query(path, data, **kwargs):
        calls.append((path, copy.deepcopy(data)))
        if path == "answer/getSaveAnswerLockResult":
            return {"state": 0}
        if path in ("student/doHomework", "student/doExam"):
            return {"state": 1, "examBase": original}
        if path == "answer/saveStudentAnswer":
            return {"statu": 1}
        if path in ("answer/getStuAnswerInfoNew", "answer/getStuAnswerInfo"):
            assert data["questionIds"] == ",".join(qids)
            return {qid: {"answer": option} for qid, option in zip(qids, options, strict=True)}
        if path == "answer/submit":
            return {"statu": 0}
        raise AssertionError(path)

    service = SharedAssessments(Mock(), TaskControl(), copy.deepcopy(AI_BASE), tmp_path,
                                budget=RequestBudget(10), emit=Mock(), log=Mock())
    service.recruit, service.school, service.course_id = "r1", "s1", "c1"
    service.query = query
    service.answer = Mock(side_effect=[[option] for option in options])
    row = {"id": "attempt1", "exam_id": "exam1", "kind": kind, "course_key": "zhidao:course1",
           "name": "测试", "state": 1, "start": "", "end": ""}
    assert service.run(row)["state"] == "submitted"
    saved = [json.loads(data["stuExamAnswer"])[0] for path, data in calls if path == "answer/saveStudentAnswer"]
    assert [(answer["eid"], answer["answer"]) for answer in saved] == list(zip(qids, options, strict=True))
    with pytest.raises(AssessmentError, match="不会重复"):
        service.run(row)
    assert sum(path == "answer/submit" for path, _ in calls) == 1


@pytest.mark.parametrize("value", ["a+b", "a/b", "a=b", "../path"])
def test_url_path_identifiers_keep_their_original_strict_validation(value):
    with pytest.raises(AssessmentError):
        ident(value)


@pytest.mark.parametrize("location", ["question", "option"])
@pytest.mark.parametrize("value", [None, True, 1.5, {}, [], "", "a,b", "a\nb", "a b", "x" * 1025])
def test_unsupported_paper_id_is_rejected_with_its_field_without_echoing_value(location, value):
    source = paper()
    question = source["workExamParts"][0]["questionDtos"][0]
    if location == "question":
        question["eid"] = value
    else:
        question["questionOptions"][0]["id"] = value
    with pytest.raises(AssessmentError, match="题目编号" if location == "question" else "选项编号"):
        questions_from_paper(source)
