"""AI course resources and knowledge-point exams, with an explicit request budget."""

import json
import logging
import math
import random
import threading
import time
from pathlib import Path

from ..ai.ppt import SlidesToText
from ..ai.providers import QUESTION_KINDS, OpenAICompatible, PlatformAI, exam_prompt, generate_answer
from ..runtime import TaskStopped
from .base import Runner
from .crypto import AI_KEY, EXAM_KEY, HOME_KEY

log = logging.getLogger("killzhs.platform")
ROOT = "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/"
EXAMS = "https://studentexamtest.zhihuishu.com/gateway/t/v1/"


class AICourse(Runner):
    def __init__(self, http, control, *, ai_config, data_dir, budget, **options):
        super().__init__(http, control, **options)
        self.ai = ai_config
        self.data_dir = Path(data_dir)
        self.budget = budget

    def list(self):
        self.http.require_login()
        self.http.ready()
        return self.http.zhidao("https://onlineservice-api.zhihuishu.com/gateway/t/v1/student/queryStudentAICourseList",
                                {"status": 3}, ok_code=None, key=HOME_KEY).get("rt")

    def query(self, path, data):
        self.http.require_login()
        self.http.ready()
        return self.http.zhidao(ROOT + path, data, ok_code=200, key=AI_KEY, content_type="json").get("data")

    def resources(self, course_id, class_id, knowledge_id):
        return self.query("resources/list-knowledge-resource", {
            "courseId": course_id, "classId": class_id, "knowledgeId": knowledge_id})

    def complete(self, course_id, class_id, knowledge_id, resource_id):
        return self.query("studyRecord/completed", {"courseId": course_id, "classId": class_id,
                          "knowledgeId": knowledge_id, "resourcesUid": resource_id, "watchUId": 1})

    def video(self, course_id, class_id, file_id, knowledge_id, *, title=None, watch_uid=1, start_at=0):
        self.check()
        result = self.query("resources-lab/get-video-time", {
            "courseId": course_id, "classId": class_id, "videoIdList": [file_id]})
        length = result[0]["time"]
        self.http.watch_video(file_id)
        played = start_at
        while played < length:
            self.check()
            played = min(int(round(played + (self.speed or 1.5) * 2)), length)
            try:
                self.query("studyRecord/report", {"courseId": course_id, "classId": class_id, "fileId": file_id,
                    "knowledgeId": knowledge_id, "lastWatchTime": played, "studyTotalTime": 10,
                    "shareCourseId": "", "nodeType": 0, "watchUId": watch_uid, "dateFormate": int(time.time() * 1000)})
            except Exception as error:
                log.error("AI 视频进度上报失败：%s", error)
            time.sleep(2)
            self.progress(played, length, title or str(file_id))

    def run(self, course_id, class_id):
        course_id, class_id = int(course_id), int(class_id)
        course = self.query("knowledge-study/course-basic", {"courseId": course_id, "classId": class_id})
        for theme in course.get("cakeThemeList") or []:
            self.check()
            log.info("主题：%s", theme.get("themeName"))
            for knowledge in theme.get("knowledgeList") or []:
                self.check()
                materials = []
                ppt_config = self.ai.get("ppt_processing", {})
                moonshot = ppt_config.get("moonShot", {})
                provide_slides = ppt_config.get("provide_to_ai", False) and moonshot.get("api_key", "")
                incomplete = (knowledge.get("studyProgress") or 0) < 100
                if incomplete or provide_slides:
                    try:
                        resources = self.resources(course_id, class_id, knowledge["knowledgeId"])
                    except Exception as error:
                        log.error("知识点资源读取失败：%s", error)
                        continue
                    for resource in resources.get("resourceList") or []:
                        detail = resource.get("resourcesDetail") or {}
                        kind = (detail.get("resourcesType"), detail.get("resourcesDistributeType"))
                        if not incomplete:
                            if kind == (1, 4):
                                materials.append({"name": detail["resourcesName"], "url": detail["resourcesUrl"]})
                            continue
                        self.check()
                        time.sleep(random.randint(1, 10) * 0.2)
                        if resource.get("studyStatus") == 1:
                            if kind == (1, 4):
                                materials.append({"name": detail["resourcesName"], "url": detail["resourcesUrl"]})
                            continue
                        try:
                            if kind in ((1, 3), (2, 2)):
                                self.video(course_id, class_id, detail["resourcesFileId"], knowledge["knowledgeId"],
                                           title=detail.get("resourcesName"))
                            else:
                                self.complete(course_id, class_id, knowledge["knowledgeId"], detail["resourcesUid"])
                                if kind == (1, 4):
                                    materials.append({"name": detail["resourcesName"], "url": detail["resourcesUrl"]})
                        except Exception as error:
                            log.error("资源运行失败：%s（%s）", detail.get("resourcesName"), error)
                attempts = 0
                while not self.ai.get("no_exam", False):
                    self.check()
                    try:
                        exam = self.query("exam/questions-paper", {"scMapId": course_id, "courseId": class_id,
                            "classId": class_id, "knowledgeId": knowledge["knowledgeId"]})
                    except Exception as error:
                        log.error("AI 课考试读取失败：%s", error)
                        break
                    if not exam or not exam.get("paperId"):
                        break
                    mastery = exam.get("masteryScore") or 0
                    if mastery < 30 and attempts > 4:
                        break
                    attempts += 1
                    if mastery > 90:
                        break
                    context = AIExam(self.http, self.control, course_id, knowledge["knowledgeId"], exam["examTestId"],
                        exam["paperId"], ai_config=self.ai, data_dir=self.data_dir, budget=self.budget,
                        extra={"courseName": course.get("courseName"), "theme": theme.get("themeName"),
                               "knowledgePoint": knowledge.get("knowledgeName")}, progress=self.progress)
                    if provide_slides:
                        converter = SlidesToText(moonshot["api_key"], moonshot.get("base_url", "https://api.moonshot.cn/v1"),
                            self.data_dir / "ppt-cache", session_factory=self.http.new_session,
                            delete_immediately=moonshot.get("delete_after_convert", True))
                        for material in materials:
                            material["content"] = converter.text(material["url"])
                    context.run(materials)
                    time.sleep(math.ceil(random.uniform(0.5, 1.5)))
            time.sleep(math.ceil(random.uniform(0.5, 1.5)))


class AIExam:
    def __init__(self, http, control, course_id, knowledge_id, exam_id, paper_id, *, ai_config, data_dir, budget,
                 extra=None, progress=lambda *_: None):
        self.http, self.control = http, control
        self.course_id, self.knowledge_id, self.exam_id, self.paper_id = course_id, knowledge_id, exam_id, paper_id
        self.extra, self.progress = extra or {}, progress
        self.path = Path(data_dir) / "ai-answers" / f"{course_id}.json"
        self.answers = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.materials = []
        self.stopped = threading.Event()
        self.heartbeat = None
        self.model_session = None
        self.model = None
        options = {"stream": ai_config.get("use_stream", False), "budget": budget, "control": control}
        if ai_config.get("enabled", False):
            if ai_config.get("use_zhidao_ai", False):
                self.model_session = http.new_session()
                self.model_session.cookies.update(http.session.cookies)
                self.model_session.headers.update(http.session.headers)
                self.model_session.proxies.update(http.proxies)
                self.model = PlatformAI(self.model_session, **options)
            else:
                settings = ai_config.get("openai", {})
                self.model = OpenAICompatible(settings.get("api_base", "https://api.openai.com/v1"),
                    settings.get("api_key", ""), settings.get("model_name", "davinci"),
                    session_factory=http.new_session, **options)

    def query(self, path, data, method="POST"):
        self.http.require_login()
        self.http.ready()
        return self.http.zhidao(EXAMS + path, data, key=EXAM_KEY, method=method).get("data")

    def retry(self, operation):
        for attempt in range(4):
            try:
                return operation()
            except Exception:
                if attempt == 3:
                    raise
                log.error("AI 课考试请求失败，正在重试（%d/3）。", attempt + 1)

    def open(self):
        self.retry(lambda: self.query("exam/user/openExam", {
            "examTestId": self.exam_id, "examPaperId": self.paper_id, "courseId": self.course_id}))
        self.heartbeat = threading.Thread(target=self.update_time, name="ai-exam-heartbeat", daemon=True)
        self.heartbeat.start()

    def update_time(self):
        while not self.stopped.is_set():
            try:
                self.query("exam/user/updateUserUsedTime", {
                    "examTestId": self.exam_id, "examPaperId": self.paper_id, "heartbeatTime": 10})
            except Exception as error:
                log.error("考试心跳失败：%s", error)
            # Cancellation is normal, including a task stop on the background thread.
            except TaskStopped:
                return
            if self.stopped.wait(10):
                return

    def question(self, question_id, version):
        try:
            return self.retry(lambda: self.query("question/getExamQuestionInfo", {"examTestId": self.exam_id,
                "examPaperId": self.paper_id, "questionId": question_id, "version": version}, "GET"))
        except Exception as error:
            log.error("题目读取失败：%s", error)
            return None

    def answer(self, question):
        key = self.cache_key(question["id"], question.get("version", 1))
        cached = self.answers.get(key)
        if cached is not None:
            time.sleep(random.randint(3, 5))
            return cached.get("answer", "").split("#@#"), "cached"
        choices = [{"id": option["id"], "content": option["content"]} for option in question["optionVos"]
                   if "id" in option and "content" in option]
        if len(choices) < 2:
            return [option["id"] for option in choices], "single option"  # B1, including an empty choice list.
        kind = question["questionType"]
        try:
            prompt = exam_prompt(QUESTION_KINDS[kind], question["content"], choices, self.materials, self.extra)
            return generate_answer(self.model, prompt), "AI generated"
        except Exception:
            if kind not in QUESTION_KINDS:
                raise ValueError(f"Unsupported question type: {kind}") from None
            selected = random.sample(choices, min(2 if kind == 2 else 1, len(choices)))  # B2: judgement too.
            time.sleep(random.randint(3, 5))
            return [item["id"] for item in selected], "random"

    @staticmethod
    def cache_key(question_id, version):
        return str(question_id) if version == 1 else f"{question_id}_{version}"

    def cache(self, question, version):
        correct = [option for option in question["optionVos"] if option.get("isCorrect", 0) == 1]
        self.answers[self.cache_key(question["id"], version)] = {"version": version,
            "question": question.get("content", ""), "answer": "#@#".join(str(option["id"]) for option in correct),
            "answer_content": "\n".join(option["content"] for option in correct), "questionDict": question}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.answers, ensure_ascii=False, indent=4), encoding="utf-8")
        temporary.replace(self.path)

    def run(self, materials=None):
        self.materials = materials or []
        try:
            self.open()
            sheet = self.retry(lambda: self.query("exam/user/getExamSheetInfo", {
                "examTestId": self.exam_id, "examPaperId": self.paper_id}, "GET"))["partSheetVos"][0]["questionSheetVos"]
            for index, item in enumerate(sheet):
                self.control.check()
                question = self.question(item["questionId"], item.get("version", 1))
                if question is None:
                    continue
                question["version"] = item.get("version", 1)
                answer, source = self.answer(question)
                if answer:
                    try:
                        self.query("answer/saveAnswer", {"recruitId": self.course_id, "examTestId": self.exam_id,
                            "examPaperId": self.paper_id, "questionId": item["questionId"], "dataVos": None,
                            "answer": "#@#".join(map(str, answer))})
                    except Exception as error:
                        log.error("AI 课答案保存失败：%s", error)
                self.progress(index + 1, len(sheet), "AI 课考试 · " + source)
            self.query("exam/user/submit", {"examTestId": self.exam_id, "courseId": self.course_id, "courseType": 8,
                "examPaperId": self.paper_id, "aiKnlowledgeId": self.knowledge_id})
            self.stopped.set()
            correct = 0
            for item in sheet:
                question = self.question(item["questionId"], item.get("version", 1))
                if question is None or not question.get("userAnswerVo"):
                    log.error("题目结果读取失败：%s", item["questionId"])
                    continue
                if question["userAnswerVo"][0]["isCorrect"] == 1:
                    correct += 1
                else:
                    log.error("Question %s is not correct", item["questionId"])
                self.cache(question, item.get("version", 1))
            return correct == len(sheet), correct, len(sheet)
        finally:
            # B3: every exit path stops the daemon, including a stop or budget exception.
            self.stopped.set()
            if self.heartbeat is not None:
                self.heartbeat.join(timeout=1)
            if self.model_session is not None:
                self.model_session.close()
