"""Platform portions of legacy test_runtime.py plus B1-B5 regressions."""

import json
import time
from unittest.mock import Mock

import pytest
import requests

from killzhs.ai import providers
from killzhs.ai.ppt import SlidesToText
from killzhs.ai.providers import AIBudgetExceeded, ChatModel, RequestBudget
from killzhs.platform.ai_course import AICourse, AIExam
from killzhs.platform.hike import HikeCourses
from killzhs.platform.shared import SharedCourses
from killzhs.runtime import TaskControl, TaskStopped


def shared_video(control=None):
    control = control or TaskControl()
    http = Mock(probe=None)
    runner = SharedCourses(http, control, speed=2, threshold=1)
    runner.context = Mock(return_value={"cookies": {}, "headers": {}, "videos": {
        1: {"studyTotalTime": 0, "watchState": 0, "name": "video", "videoSec": 100, "videoId": 1}}})
    runner.prelearning = Mock(return_value={"studiedLessonDto": {"id": 1}})
    runner.pointers = Mock(return_value={"questionPoint": []})
    return runner


def test_shared_video_flushes_only_unsent_progress_on_stop(monkeypatch):
    runner = shared_video()
    submitted, ticks = [], []

    def report(*args):
        runner.control.check()
        submitted.append(args)

    def sleep(_):
        ticks.append(1)
        if len(ticks) == 2:
            runner.control.deadline = time.monotonic() - 1
            raise TaskStopped("limited")

    runner.report = report
    monkeypatch.setattr("killzhs.platform.shared.time.sleep", sleep)
    monkeypatch.setattr("killzhs.platform.shared.random.random", lambda: 1)
    with pytest.raises(TaskStopped):
        runner.video("course", 1)
    assert len(submitted) == 1
    assert submitted[0][2:4] == (2, 0)
    assert not runner.control.cleaning


@pytest.mark.parametrize(("length", "threshold", "endpoint"), [(2, 1, 2), (7, .91, 7)],
                         ids=["exact_endpoint", "fractional_threshold"])
def test_hike_video_exits_without_repeating_integer_progress(monkeypatch, length, threshold, endpoint):
    runner = HikeCourses(Mock(probe=None), TaskControl(), speed=2, threshold=threshold)
    runner.context = Mock(return_value={"cookies": {}, "headers": {}})
    runner.view = Mock(return_value={"dataId": 1, "totalTime": length})
    runner.report = Mock(side_effect=[endpoint, AssertionError("Repeated endpoint submission")])
    monkeypatch.setattr("killzhs.platform.hike.time.sleep", lambda _: None)
    runner.video("course", "video")
    runner.report.assert_called_once()
    assert runner.report.call_args.args[2] == endpoint


def test_ai_video_checks_cancellation_between_reports(tmp_path, monkeypatch):
    course = AICourse(Mock(), TaskControl(), ai_config={}, data_dir=tmp_path, budget=RequestBudget(10))
    course.query = Mock(side_effect=[[{"time": 100}], {}])

    def stop(_):
        raise TaskStopped("limited")

    monkeypatch.setattr("killzhs.platform.ai_course.time.sleep", stop)
    with pytest.raises(TaskStopped):
        course.video(1, 2, 3, 4)
    assert [call.args[0] for call in course.query.call_args_list] == ["resources-lab/get-video-time", "studyRecord/report"]


def test_ppt_remote_delete_runs_when_extraction_unwinds_on_stop():
    converter = object.__new__(SlidesToText)
    converter.delete_immediately, converter.uploads = True, {}
    converter.client = Mock()
    converter.client.files.content.side_effect = TaskStopped("limited")
    with pytest.raises(TaskStopped):
        converter.extract("test-upload")
    converter.client.files.delete.assert_called_once_with(file_id="test-upload")


def exam(tmp_path):
    return AIExam(Mock(), TaskControl(), 1, 2, 3, 4, ai_config={}, data_dir=tmp_path, budget=RequestBudget(10))


@pytest.mark.parametrize("choices", [[], [{"id": 123, "content": "only"}]])
def test_b1_fewer_than_two_options_always_returns_answer_and_source(tmp_path, choices):
    answer, source = exam(tmp_path).answer({"id": 1, "questionType": 1, "content": "?", "optionVos": choices})
    assert answer == [option["id"] for option in choices]
    assert isinstance(source, str)


@pytest.mark.parametrize("kind,count", [(1, 1), (2, 2), (14, 1)])
def test_b2_model_failure_uses_random_fallback_including_judgement(tmp_path, monkeypatch, kind, count):
    monkeypatch.setattr("killzhs.platform.ai_course.generate_answer", Mock(side_effect=ValueError("no answer")))
    monkeypatch.setattr("killzhs.platform.ai_course.time.sleep", lambda _: None)
    answer, source = exam(tmp_path).answer({"id": 1, "questionType": kind, "content": "?",
        "optionVos": [{"id": 2, "content": "A"}, {"id": 3, "content": "B"}]})
    assert len(answer) == count and set(answer) <= {2, 3}
    assert source == "random"


@pytest.mark.parametrize("failure", [ValueError("sheet failed"), TaskStopped("limited"), AIBudgetExceeded()])
def test_b3_exam_heartbeat_stops_on_every_exit(tmp_path, failure):
    context = exam(tmp_path)

    def query(path, *args):
        if path == "exam/user/getExamSheetInfo":
            raise failure
        return {}

    context.query = query
    with pytest.raises(type(failure)):
        context.run()
    assert context.stopped.is_set()
    assert context.heartbeat.daemon
    assert not context.heartbeat.is_alive()


def test_b4_stream_without_answer_returns_received_text_and_clear_parse_error(monkeypatch):
    response = Mock()
    response.iter_lines.return_value = iter([b'data: {"choices":[{"delta":{"content":"partial response"}}]}',
                                            b"data: [DONE]"])
    result = ChatModel.read_stream(response, "```answer", "```")
    assert result == "partial response"
    monkeypatch.setattr(providers, "encoder", lambda: Mock(encode=lambda text: [1]))
    with pytest.raises(ValueError, match="Answer block not found"):
        providers.generate_answer(Mock(complete=lambda _: result), "prompt")


def test_b5_missing_small_lesson_id_uses_parent_id_without_overwriting_explicit_id():
    http = Mock()
    http.snapshot.return_value = {}
    runner = SharedCourses(http, TaskControl())
    runner.query = Mock(side_effect=[
        {"recruitId": 9}, {"courseId": 10, "videoChapterDtos": [{"id": 8, "videoLessons": [{"id": 7,
            "videoSmallLessons": [{"id": 5, "videoId": 1}, {"id": 6, "videoId": 2, "lessonId": 70}]}]}]},
        {}, {}, {}])
    videos = runner.context("course")["videos"]
    assert videos[1]["lessonId"] == 7
    assert videos[2]["lessonId"] == 70
    runner.prelearning(runner.context("course"), videos[1])
    assert runner.query.call_args.args[1]["lessonId"] == 7


def test_budget_counts_every_http_attempt_and_cannot_be_swallowed(monkeypatch):
    uses = []
    budget = RequestBudget(2, on_use=uses.append)
    model = ChatModel(stream=False, budget=budget)
    send = Mock(side_effect=requests.Timeout("offline"))
    model.prepare = lambda _: send
    monkeypatch.setattr(providers.time, "sleep", lambda _: None)
    with pytest.raises(AIBudgetExceeded):
        model.complete("prompt")
    assert send.call_count == 2
    assert uses == [1, 2] and budget.remaining == 0


def test_ai_exam_cache_is_shared_by_course_but_isolated_by_question_version(tmp_path):
    context = exam(tmp_path)
    question = {"id": 11, "optionVos": [{"id": 21, "content": "A", "isCorrect": 1}]}
    context.cache(question, 1)
    context.cache({**question, "content": "new version"}, 2)
    restored = exam(tmp_path)
    assert set(restored.answers) == {"11", "11_2"}
    assert json.loads(context.path.read_text(encoding="utf-8"))["11_2"]["question"] == "new version"


def test_provider_closes_stream_when_answer_arrives_before_end_of_body():
    response = Mock()

    def lines():
        yield b'data: {"choices":[{"delta":{"content":"```answer\\n[]\\n```"}}]}'
        raise AssertionError("The stream should stop at the complete answer block")

    response.iter_lines.side_effect = lines
    model = ChatModel(stream=True, budget=RequestBudget(1))
    model.prepare = Mock(return_value=lambda: response)
    assert model.complete("prompt") == "```answer\n[]\n```"
    response.close.assert_called_once()


@pytest.mark.parametrize("stream", [False, True])
def test_provider_closes_response_when_parsing_raises(stream):
    response = Mock()
    model = ChatModel(stream=stream, budget=RequestBudget(1))
    model.prepare = Mock(return_value=lambda: response)
    parser = Mock(side_effect=ValueError("invalid response"))
    if stream:
        model.read_stream = parser
    else:
        model.read = parser
    with pytest.raises(ValueError, match="invalid response"):
        model.complete("prompt")
    response.close.assert_called_once()


def test_provider_closes_failed_http_response_before_retry_and_final_failure(monkeypatch):
    responses = [Mock(), Mock(), Mock()]
    for response in responses:
        response.raise_for_status.side_effect = requests.HTTPError("offline HTTP failure")
    sent = []

    def send():
        if sent:
            sent[-1].close.assert_called_once()
        response = responses[len(sent)]
        sent.append(response)
        return response

    model = ChatModel(stream=True, budget=RequestBudget(3))
    model.prepare = Mock(return_value=send)
    monkeypatch.setattr(providers.time, "sleep", lambda _: None)
    with pytest.raises(requests.HTTPError, match="offline HTTP failure"):
        model.complete("prompt")
    assert len(sent) == 3
    for response in responses:
        response.close.assert_called_once()


@pytest.mark.parametrize("line", [b"", b": keepalive", b'data: {"choices":[{"delta":{"content":"text"}}]}'])
def test_stream_cancellation_escapes_closes_response_and_consumes_no_retry_budget(line):
    stopped = [False]
    control = TaskControl(stop_signal=lambda: "stopped" if stopped[0] else None)
    response = Mock()

    def lines():
        stopped[0] = True
        yield line
        raise AssertionError("A cancelled stream must not read another line")

    response.iter_lines.side_effect = lines
    uses = []
    budget = RequestBudget(3, on_use=uses.append)
    send = Mock(return_value=response)
    model = ChatModel(stream=True, budget=budget, control=control)
    model.prepare = Mock(return_value=send)
    with pytest.raises(TaskStopped) as caught:
        model.complete("prompt")
    assert caught.value.reason == "stopped"
    response.close.assert_called_once()
    send.assert_called_once()
    assert uses == [1] and budget.used == 1 and budget.remaining == 2
