"""Campus courses (校内课): list, resource tree traversal and signed progress reports."""

import logging
import math
import random
import time
from datetime import UTC, datetime

from ..runtime import TaskStopped
from .base import Runner

log = logging.getLogger("killzhs.platform")

RESOURCES = "https://studyresources.zhihuishu.com/studyResources/stuResouce/"
DEFAULT_SPEED = 1.25
REPORT_INTERVAL = 30
VIDEO = 3


class HikeCourses(Runner):
    def __init__(self, http, control, **options):
        super().__init__(http, control, **options)
        self.contexts = {}

    def list(self):
        self.http.require_login()
        self.http.ready()
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        result = self.http.api("https://hikeservice.zhihuishu.com/student/course/aided/getMyCourseList",
                               {"uuid": self.http.uuid, "data": stamp}, "GET")["result"]
        return result.get("startInngcourseList") or []

    def context(self, course_id):
        if course_id in self.contexts:
            return self.contexts[course_id]
        self.http.require_login()
        self.http.ready()
        self.http.session.headers.update({"Origin": "https://hike.zhihuishu.com", "Referer": "https://hike.zhihuishu.com/"})
        root = self.http.hike(RESOURCES + "queryResourceMenuTree", {"courseId": course_id}).get("rt")
        self.contexts[course_id] = context = {"root": root, **self.http.snapshot()}
        return context

    def run(self, course_id):
        begin = time.time()
        root = self.context(course_id)["root"]
        log.info("开始校内课：%s，共 %d 个顶层章节。", course_id, len(root))
        for node in root:
            self.visit(course_id, node)
        log.info("校内课本次运行结束：%s，用时 %.0f 秒。", course_id, time.time() - begin)

    def visit(self, course_id, node):
        self.check()
        if node.get("childList"):
            log.info("章节：%s", node.get("name"))
            for child in node["childList"]:
                self.visit(course_id, child)
            return
        studied = node.get("studyTime") or 0
        if studied >= (node.get("totalTime") or 0) * self.threshold:
            return
        log.info("资源：%s", node.get("name"))
        try:
            if node.get("dataType") == VIDEO:
                self.video(course_id, node["id"], studied, title=node.get("name"))
            elif node.get("dataType") is None:
                log.info("不支持的资源类型，可能是测验：%s", node.get("name"))
            else:
                self.view(course_id, node["id"])
                time.sleep(random.random() * 2 + 1)
        except Exception as error:
            log.error("资源运行失败：%s（%s）", node.get("name"), error)

    def video(self, course_id, file_id, prev_time=0, *, title=None):
        flush = None
        try:
            self.check()
            self.http.require_login()
            context = self.context(course_id)
            self.http.ready(context["cookies"], context["headers"])
            info = self.view(course_id, file_id)
            self.http.watch_video(info["dataId"])
            start_date = int(time.time() * 1000)
            speed = self.speed or DEFAULT_SPEED
            # The platform records whole seconds; a fractional target could never be confirmed.
            end_time = max(math.ceil(int(info["totalTime"]) * self.threshold), 1)
            played_time = prev_time

            def flush():
                if played_time > prev_time:
                    self.report(course_id, file_id, played_time, prev_time, start_date)

            while played_time < end_time:
                self.check()
                time.sleep(1)
                self.check()
                played_time = min(played_time + speed, end_time)
                if played_time >= end_time or not int(played_time - prev_time) % REPORT_INTERVAL:
                    prev_time = played_time = self.report(course_id, file_id, played_time, prev_time, start_date)
                self.progress(played_time, end_time, title or info.get("name") or str(file_id))
            time.sleep(random.random() + 1)
        except TaskStopped:
            self.flush_after_stop(flush)
            raise

    def view(self, course_id, file_id):
        return self.http.hike(RESOURCES + "stuViewFile", {"courseId": course_id, "fileId": file_id})["rt"]

    def report(self, course_id, file_id, played_time, prev_time, start_date):
        result = self.http.hike("https://hike-teaching.zhihuishu.com/stuStudy/saveStuStudyRecord", {
            "uuid": self.http.uuid, "courseId": course_id, "fileId": file_id,
            "studyTotalTime": int(played_time - prev_time), "startWatchTime": int(prev_time),
            "endWatchTime": int(played_time), "startDate": start_date, "endDate": int(time.time() * 1000),
        }, sign=True).get("rt")
        if result is None:
            raise ValueError("Failed to save study record")
        return result
