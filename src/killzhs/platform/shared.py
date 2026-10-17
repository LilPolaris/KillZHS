"""Shared courses (共享课): list, course context and video playback with pop-up quizzes.

The playback loop mirrors the official player: one simulated second per real
second, a progress report every 30 seconds, on pop-up quizzes and at the end,
and a rare one-minute pause. The order of random draws is part of that
behaviour and is covered by the protocol traces.
"""

import base64
import logging
import math
import random
import time

from ..runtime import TaskStopped
from .base import Runner
from .crypto import HOME_KEY, WatchPoints, encode_ev, hms
from .http import CaptchaRequired

log = logging.getLogger("killzhs.platform")

ONLINE = "https://onlineservice-api.zhihuishu.com/gateway/t/v1/"
STUDY = "https://studyservice-api.zhihuishu.com/gateway/t/v1/"
DEFAULT_SPEED = 1.5
REPORT_INTERVAL = 30
PAUSE_CHANCE = 0.0025


class SharedCourses(Runner):
    def __init__(self, http, control, **options):
        super().__init__(http, control, **options)
        self.contexts = {}

    def query(self, path, data, *, method="POST", **options):
        return self.http.zhidao(STUDY + path, data, method=method, **options).get("data")

    def list(self):
        self.http.require_login()
        self.http.ready()
        url = ONLINE + "student/course/share/queryShareCourseInfo"
        data = {"status": 0, "pageNo": 1, "pageSize": 5}
        result = self.http.zhidao(url, data, ok_code=200, key=HOME_KEY)["result"]
        courses = result.get("courseOpenDtos") or []
        for page in range(2, math.ceil((result.get("totalCount") or 0) / 5) + 1):
            data["pageNo"] = page
            courses += self.http.zhidao(url, data, ok_code=200, key=HOME_KEY)["result"].get("courseOpenDtos") or []
        return courses

    # ---- course context -------------------------------------------------
    def context(self, course_id):
        if course_id in self.contexts:
            return self.contexts[course_id]
        self.http.require_login()
        log.info("正在读取共享课信息：%s", course_id)
        self.http.ready()
        self.http.session.headers.update({"Origin": "https://studyh5.zhihuishu.com",
                                          "Referer": "https://studyh5.zhihuishu.com/"})
        self.http.session.get("https://studyservice-api.zhihuishu.com/login/gologin", params={
            "fromurl": f"https://studyh5.zhihuishu.com/videoStudy.html#/studyVideo?recruitAndCourseId={course_id}"},
            proxies=self.http.proxies, timeout=10)
        course = self.query("learning/queryCourse", {"recruitAndCourseId": course_id})
        chapters = self.query("learning/videolist", {"recruitAndCourseId": course_id})
        lesson_ids, videos = [], {}
        for chapter in chapters.get("videoChapterDtos") or []:
            for lesson in chapter.get("videoLessons") or []:
                lesson_ids.append(lesson.get("id"))
                if "videoId" in lesson:  # A lesson that is itself one video.
                    lesson["videoSmallLessons"] = [{**lesson, "lessonId": lesson.get("id"), "id": 0}]
                for video in lesson.get("videoSmallLessons") or []:
                    video.setdefault("lessonId", lesson.get("id"))
                    video["chapterId"] = chapter.get("id")
                    videos[video["videoId"]] = video
        log.info("共 %d 个课时、%d 个视频。", len(lesson_ids), len(videos))
        states = self.study_info(lesson_ids, [video["id"] for video in videos.values() if video.get("id")],
                                 course["recruitId"])
        for video in videos.values():
            state = ((states.get("lv") or {}).get(str(video.get("id")))
                     or (states.get("lesson") or {}).get(str(video.get("lessonId"))) or {})
            video["watchState"], video["studyTotalTime"] = state.get("watchState"), state.get("studyTotalTime") or 0
        self.query("learning/queryUserRecruitIdLastVideoId", {"recruitId": course["recruitId"]})
        self.contexts[course_id] = context = {"id": course_id, "course": course, "chapters": chapters,
                                              "videos": videos, **self.http.snapshot()}
        return context

    def study_info(self, lesson_ids, video_ids, recruit_id):
        return self.query("learning/queryStuyInfo", {"lessonIds": lesson_ids, "lessonVideoIds": video_ids,
                                                     "recruitId": recruit_id})

    # ---- playback -------------------------------------------------------
    def run(self, course_id):
        context = self.context(course_id)
        info = context["course"].get("courseInfo") or {}
        name = info.get("name") or info.get("enName")
        log.info("开始共享课：%s", name)
        begin = time.time()
        for chapter in context["chapters"].get("videoChapterDtos") or []:
            log.info("章节：%s", chapter.get("name"))
            for lesson in chapter.get("videoLessons") or []:
                for video in lesson.get("videoSmallLessons") or []:
                    log.info("视频：%s", video.get("name"))
                    try:
                        self.video(course_id, video["videoId"])
                    except CaptchaRequired:
                        self.http.issues.add("verification")
                        log.info("平台要求验证，本门课程已停止。")
                        return
                    except Exception as error:
                        log.error("视频运行失败：%s", error)
        log.info("共享课本次运行结束：%s，用时 %.0f 秒。", name, time.time() - begin)

    def video(self, course_id, video_id):
        flush = None
        try:
            self.http.video = {"course_id": course_id, "video_id": video_id, "played_time": None}
            self.check()
            self.http.require_login()
            context = self.context(course_id)
            self.http.ready(context["cookies"], context["headers"])
            video = context["videos"][video_id]
            played_time = video["studyTotalTime"]
            self.http.video["played_time"] = played_time
            if video.get("watchState") == 1 and self.threshold <= 1.0:  # A threshold above 1 rewatches.
                log.info("已看完，跳过：%s", video.get("name"))
                return
            token = base64.b64encode(str(self.prelearning(context, video)["studiedLessonDto"]["id"]).encode()).decode()
            questions = sorted(self.pointers(context, video).get("questionPoint") or [],
                               key=lambda point: point["timeSec"], reverse=True)
            while questions and questions[-1]["timeSec"] <= played_time:
                questions.pop()  # Already answered before this run.
            end_time = max(video["videoSec"] * self.threshold, 1.0)
            if questions:
                end_time = max(questions[0]["timeSec"], end_time)  # Stay until the last quiz is answered.
            self.http.watch_video(video["videoId"])
            self.query("course/threeDimensionalCourseWare", {"videoId": video["videoId"]}, method="GET")

            speed = self.speed or DEFAULT_SPEED
            last_submit, elapsed, pause = played_time, 0, 0
            answer, question, report = None, None, False
            marks = WatchPoints()

            def flush():
                if played_time > last_submit:
                    marks.add(played_time)
                    self.report(context, video, played_time, last_submit, str(marks), token)

            while played_time < end_time:
                self.check()
                time.sleep(1)
                self.check()
                elapsed += 1
                played_time = min(played_time + speed, end_time)
                pause = pause or int(random.random() < PAUSE_CHANCE) * 60
                report = report or pause == 60
                if pause:
                    pause -= 1
                    played_time = last_submit
                self.http.video["played_time"] = played_time
                if not elapsed % 2:
                    marks.add(played_time)
                if questions and played_time >= questions[-1]["timeSec"]:
                    question = questions.pop()
                    try:
                        question = self.popup(context, video, question["questionIds"])[
                            "lessonTestQuestionUseInterfaceDtos"][0]["testQuestion"]
                        answer, report = 2, True  # Answer after a two-second delay.
                    except CaptchaRequired:
                        raise
                    except Exception as error:
                        log.error("无法读取弹题：%s", error)
                if answer is not None:
                    if answer == 0:
                        answer = None
                        self.save_answer(context, video, question.get("questionId"), self.choose(question))
                        log.info("已回答弹题。")
                    else:
                        pause = pause or 1  # The player pauses while a quiz is open.
                        answer -= 1
                if elapsed % REPORT_INTERVAL == 0 or played_time >= end_time or report:
                    report = False
                    marks.add(played_time)
                    self.report(context, video, played_time, last_submit, str(marks), token)
                    last_submit = played_time
                    marks.reset(played_time)
                if pause:
                    self.progress(60 - pause, 60, "短暂暂停，等待继续", waiting=True)
                else:
                    self.progress(played_time, end_time, "正在回答弹题" if answer is not None else video.get("name"))
            time.sleep(random.random() + 1)
        except TaskStopped:
            self.flush_after_stop(flush)
            raise

    @staticmethod
    def choose(question):
        return ",".join(str(option["id"]) for option in question.get("questionOptions") or []
                        if option.get("result") == "1")

    # ---- requests -------------------------------------------------------
    def prelearning(self, context, video):
        return self.query("learning/prelearningNote", {
            "ccCourseId": context["chapters"]["courseId"], "chapterId": video["chapterId"], "isApply": 1,
            "lessonId": video["lessonId"], "lessonVideoId": video["id"], "recruitId": context["course"]["recruitId"],
            "videoId": video["videoId"]})

    def pointers(self, context, video):
        return self.query("popupAnswer/loadVideoPointerInfo", {
            "lessonId": video["lessonId"], "lessonVideoId": video["id"], "recruitId": context["course"]["recruitId"],
            "courseId": context["chapters"]["courseId"]})

    def popup(self, context, video, question_ids):
        return self.query("popupAnswer/lessonPopupExam", {
            "lessonId": video["lessonId"], "lessonVideoId": video["id"], "questionIds": question_ids})

    def save_answer(self, context, video, question_id, answer):
        return self.query("popupAnswer/saveLessonPopupExamSaveAnswer", {
            "courseId": context["chapters"]["courseId"], "recruitId": context["course"]["recruitId"],
            "testQuestionId": question_id, "isCurrent": "1", "lessonId": video["lessonId"],
            "lessonVideoId": video["id"], "answer": answer, "testType": 0})

    def report(self, context, video, played_time, last_submit, watch_point, token):
        self.http.video = {"course_id": context["id"], "video_id": video["videoId"], "played_time": played_time}
        course = context["course"]
        values = [course["recruitId"], video["lessonId"], video["id"], video["videoId"], video["chapterId"], "0",
                  int(played_time - last_submit), int(played_time), hms(min(video["videoSec"], int(played_time))),
                  self.http.uuid + "zhs"]
        return self.query("learning/saveDatabaseIntervalTimeV2", {
            "ewssw": watch_point, "sdsew": encode_ev(values), "zwsds": token,
            "courseId": course["courseInfo"]["courseId"]})
