"""Offline Zhihuishu double that answers like the platform and records every request.

Shared by ``scripts/record_legacy.py`` (run once against the old project to
produce the golden files) and by this project's protocol tests. It replaces
``requests.Session.request`` so nothing ever reaches the network, and a fake
clock so loops that sleep for minutes finish instantly and deterministically.
"""

import json
import re
import threading
import time
from base64 import b64decode, b64encode
from urllib.parse import parse_qsl, quote, urlsplit

import requests
from Crypto.Cipher import AES
from requests.structures import CaseInsensitiveDict

IV = b"1g3qqdh4jvbskb9x"
KEYS = {"video": b"azp53h0kft7qi78q", "home": b"7q9oko0vqb3la20r", "ai": b"hw2fdlwcj4cs1mx7",
        "exam": b"onbfhdyvz8x7otrp", "qa": b"kcGOlISPkYKRksSK"}
UUID = "U-fake-0001"
CASLOGC = quote(json.dumps({"uuid": UUID, "realName": "测试"}))

SHARED_ID = "a1b2c3d4e5"
HIKE_ID = "3001"
# Public example from upstream zd_utils.py (its standalone encryption demo),
# not an account's course enrollment. Every response and cookie here is synthetic.
AI_COURSE_ID, AI_CLASS_ID = "1839576198548688896", "4239"

# Values that legitimately change between runs and carry no protocol meaning.
VOLATILE_QUERY = {"/student/course/aided/getMyCourseList": {"data"}}
BACKGROUND_VOLATILE = {"_", "dateFormate"}
REAL_SLEEP = time.sleep


def decrypt(text):
    raw = b64decode(text)
    for name, key in KEYS.items():
        try:
            plain = AES.new(key, AES.MODE_CBC, IV).decrypt(raw).decode()
            plain = plain[:-ord(plain[-1])]
            json.loads(plain)
            return name, plain
        except (ValueError, UnicodeDecodeError, IndexError):
            continue
    raise ValueError("secretStr cannot be decrypted with any known key")


class Clock:
    """Main-thread sleeps advance time; background threads yield briefly instead.

    Before time moves, threads started during the scenario get a moment to
    finish, so a short-lived helper thread always sends its requests at the
    same fake time instead of racing the main loop past a deadline.
    """

    def __init__(self, start=1_760_000_000.0):
        self.now, self.mono = start, 50_000.0
        self.baseline = set(threading.enumerate())

    def time(self):
        return self.now

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        current = threading.current_thread()
        if current is threading.main_thread():
            for thread in threading.enumerate():
                if thread not in self.baseline and thread is not current:
                    thread.join(0.05)
            self.now += seconds
            self.mono += seconds
        else:
            REAL_SLEEP(0.002)


def make_response(prepared, payload=None, *, text=None, status=200, url=None, history=()):
    response = requests.Response()
    response.status_code = status
    response.reason = "OK" if status < 400 else "Error"
    response.url = url or prepared.url
    response.request = prepared
    response.history = list(history)
    response.encoding = "utf-8"
    body = text if text is not None else json.dumps(payload, ensure_ascii=False)
    response._content = body.encode("utf-8")
    response._content_consumed = True
    response.headers = CaseInsensitiveDict({"Content-Type": "application/json" if text is None else "text/plain"})
    return response


def normalize(prepared, *, timeout, allow_redirects, stream):
    """A comparable view of one request: decrypted body, cookie names, merged headers."""
    parts = urlsplit(prepared.url)
    headers = {key.lower(): value for key, value in prepared.headers.items() if key.lower() != "cookie"}
    cookies = sorted(item.split("=", 1)[0].strip() for item in prepared.headers.get("Cookie", "").split(";") if item.strip())
    body = prepared.body.decode("utf-8") if isinstance(prepared.body, bytes) else prepared.body
    entry = {"method": prepared.method, "url": f"{parts.scheme}://{parts.netloc}{parts.path}",
             "query": [list(pair) for pair in parse_qsl(parts.query, keep_blank_values=True)],
             "headers": dict(sorted(headers.items())), "cookies": cookies,
             "timeout": timeout, "allow_redirects": allow_redirects, "stream": bool(stream)}
    volatile = VOLATILE_QUERY.get(parts.path, set())
    entry["query"] = [[key, "<volatile>" if key in volatile else value] for key, value in entry["query"]]
    fields = dict(entry["query"])
    if body is None:
        entry["body"] = None
    else:
        try:
            entry["body"] = {"json": json.loads(body)}
            if isinstance(entry["body"]["json"], dict):
                fields = entry["body"]["json"]
        except ValueError:
            entry["body"] = {"form": [list(pair) for pair in parse_qsl(body, keep_blank_values=True)]}
            fields = dict(entry["body"]["form"])
    if isinstance(fields.get("secretStr"), str):
        key, plain = decrypt(fields["secretStr"])
        entry["secret"] = {"key": key, "plain": plain}
    return entry


def background_view(entry):
    """Background threads run at nondeterministic moments; compare them without timestamps."""
    view = {"method": entry["method"], "url": entry["url"],
            "query": [pair for pair in entry["query"] if pair[0] not in BACKGROUND_VOLATILE]}
    if "secret" in entry:
        data = json.loads(entry["secret"]["plain"])
        view["secret"] = {key: value for key, value in data.items() if key not in BACKGROUND_VOLATILE}
    return view


class Transport:
    """Install once per process; scenarios swap the platform and clock objects."""

    def __init__(self):
        self.platform = None
        self.before_request = None  # legacy worker emulation hook
        self.original = requests.sessions.Session.request
        self.original_time = (time.time, time.sleep, time.monotonic)

    def install(self, clock):
        self.clock = clock
        transport = self

        def request(session, method, url, params=None, data=None, headers=None, cookies=None, files=None,
                    auth=None, timeout=None, allow_redirects=True, proxies=None, hooks=None, stream=None,
                    verify=None, cert=None, json=None):
            if transport.before_request is not None:
                timeout = transport.before_request(url, timeout)
            prepared = session.prepare_request(requests.Request(
                method=method.upper(), url=url, headers=headers, files=files, data=data or {}, json=json,
                params=params or {}, auth=auth, cookies=cookies, hooks=hooks))
            entry = normalize(prepared, timeout=timeout, allow_redirects=allow_redirects, stream=stream)
            background = threading.current_thread() is not threading.main_thread()
            return transport.platform.handle(session, prepared, entry, background=background)

        requests.sessions.Session.request = request
        time.time, time.sleep, time.monotonic = clock.time, clock.sleep, clock.monotonic


class Platform:
    """Course fixtures and endpoint behaviour. Options select scenario variants."""

    def __init__(self, *, qr_expire_first=False, captcha_on_report=False):
        self.requests, self.background = [], []
        self.unhandled = []
        self.lock = threading.Lock()
        self.qr_expire_first = qr_expire_first
        self.captcha_on_report = captcha_on_report
        self.qr_count = 0
        self.qr_polls = {}
        self.mastery = {41: [40, 40, 95], 42: [95]}
        self.exam_submissions = {}
        self.saved_answers = {}

    # ---- dispatch -------------------------------------------------------
    def handle(self, session, prepared, entry, *, background):
        with self.lock:
            (self.background if background else self.requests).append(entry)
        parts = urlsplit(prepared.url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        secret = json.loads(entry["secret"]["plain"]) if "secret" in entry else None
        body = entry["body"]["json"] if entry.get("body") and "json" in entry["body"] else None
        handler = ROUTES.get((parts.netloc, parts.path))
        if handler is None:
            if parts.netloc.endswith("zhihuishu.com") and parts.path.startswith(("/studentExam/", "/taurusExam/")):
                handler = Platform.assessment
            elif parts.netloc == "video.example":
                return make_response(prepared, text="")
            elif parts.path.endswith("/chat/completions"):
                handler = Platform.openai
        if handler is None:
            self.unhandled.append(entry)
            return make_response(prepared, {"code": 404, "message": "not found"}, status=404)
        return handler(self, session, prepared, query=query, secret=secret, body=body)

    # ---- login ----------------------------------------------------------
    def qr_image(self, session, prepared, **_):
        self.qr_count += 1
        token = f"qr-{self.qr_count}"
        expire = self.qr_expire_first and self.qr_count == 1
        self.qr_polls[token] = [-1, 2] if expire else [-1, 0, 1]
        return make_response(prepared, {"qrToken": token, "img": b64encode(f"PNG-{self.qr_count}".encode()).decode()})

    def qr_info(self, session, prepared, query, **_):
        token = query["qrToken"]
        states = self.qr_polls[token]
        status = states.pop(0) if len(states) > 1 else states[0]
        reply = {"status": status, "msg": {-1: "等待扫码", 0: "已扫码", 1: "已确认", 2: "二维码已过期"}[status]}
        if status == 1:
            reply["oncePassword"] = "otp-" + token
        return make_response(prepared, reply)

    def login_page(self, session, prepared, query, **_):
        if "pwd" in query:
            session.cookies.set("CASLOGC", CASLOGC, domain=".zhihuishu.com", path="/")
            session.cookies.set("CASTGC", "TGT-fake", domain=".zhihuishu.com", path="/")
        return make_response(prepared, text="<html>ok</html>")

    def cas(self, session, prepared, query, **_):
        service = query["service"]
        redirect = make_response(prepared, text="", status=302)
        return make_response(prepared, text="method not allowed", status=405, url=service, history=[redirect])

    # ---- course lists ---------------------------------------------------
    def share_list(self, session, prepared, secret, **_):
        courses = [{"secret": SHARED_ID if index == 1 else f"shared{index}x", "courseName": f"共享课{index}"}
                   for index in range(1, 8)]
        page = secret["pageNo"]
        return make_response(prepared, {"code": 200, "result": {"totalCount": len(courses),
                                        "courseOpenDtos": courses[(page - 1) * 5: page * 5]}})

    def hike_list(self, session, prepared, **_):
        return make_response(prepared, {"result": {"startInngcourseList": [
            {"courseId": int(HIKE_ID), "courseName": "示例校内课"}]}})

    def ai_list(self, session, prepared, **_):
        return make_response(prepared, {"code": 0, "rt": [
            {"courseId": int(AI_COURSE_ID), "courseName": "示例AI课", "classId": int(AI_CLASS_ID)}]})

    # ---- shared course --------------------------------------------------
    def ok(self, session, prepared, **_):
        return make_response(prepared, text="ok")

    def query_course(self, session, prepared, **_):
        return self.zhidao(prepared, {"recruitId": 9001, "courseInfo": {"name": "示例共享课", "enName": "", "courseId": 7101}})

    def video_list(self, session, prepared, **_):
        return self.zhidao(prepared, {"courseId": 7001, "videoChapterDtos": [
            {"id": 11, "name": "第一章", "videoLessons": [
                {"id": 101, "name": "1.1 单视频课", "videoId": 5001, "videoSec": 40},
                {"id": 102, "name": "1.2 多小节", "videoSmallLessons": [
                    {"id": 1021, "lessonId": 102, "name": "1.2.1 小节", "videoId": 5002, "videoSec": 30},
                    {"id": 1022, "lessonId": 102, "name": "1.2.2 已看完", "videoId": 5003, "videoSec": 25}]}]},
            {"id": 12, "name": "第二章", "videoLessons": [
                {"id": 103, "name": "2.1 长视频", "videoId": 5004, "videoSec": 600}]}]})

    def study_info(self, session, prepared, **_):
        return self.zhidao(prepared, {
            "lv": {"1021": {"watchState": 0, "studyTotalTime": 10}, "1022": {"watchState": 1, "studyTotalTime": 25}},
            "lesson": {"101": {"watchState": 0, "studyTotalTime": 0}, "103": {"watchState": 0, "studyTotalTime": 12}}})

    def empty(self, session, prepared, **_):
        return self.zhidao(prepared, {})

    def prelearning(self, session, prepared, secret, **_):
        return self.zhidao(prepared, {"studiedLessonDto": {"id": 770000 + secret["videoId"] % 1000}})

    def pointers(self, session, prepared, secret, **_):
        points = None
        if secret["lessonId"] == 103:
            points = [{"timeSec": 400, "questionIds": [62]}, {"timeSec": 5, "questionIds": [60]},
                      {"timeSec": 30, "questionIds": [61]}]
        return self.zhidao(prepared, {"questionPoint": points})

    def popup(self, session, prepared, secret, **_):
        question = secret["questionIds"][0]
        return self.zhidao(prepared, {"lessonTestQuestionUseInterfaceDtos": [{"testQuestion": {
            "questionId": question, "questionOptions": [
                {"id": question * 10 + 1, "result": "0"}, {"id": question * 10 + 2, "result": "1"},
                {"id": question * 10 + 3, "result": "1"}]}}]})

    def report(self, session, prepared, **_):
        if self.captcha_on_report:
            return make_response(prepared, {"code": -12, "message": "请完成安全验证"})
        return self.zhidao(prepared, {"submitSuccess": True})

    def init_video(self, session, prepared, query, **_):
        line = {"result": {"lines": [{"lineUrl": f"https://video.example/{query['videoID']}.mp4"}]}}
        return make_response(prepared, text="result(" + json.dumps(line) + ")")

    @staticmethod
    def zhidao(prepared, data):
        return make_response(prepared, {"code": 0, "message": "成功", "data": data})

    # ---- campus course --------------------------------------------------
    def menu_tree(self, session, prepared, **_):
        return make_response(prepared, {"status": 200, "rt": [{"id": 1, "name": "第一单元", "childList": [
            {"id": 31, "name": "视频一", "dataType": 3, "studyTime": 0, "totalTime": 50},
            {"id": 32, "name": "讲义", "dataType": 1, "studyTime": None, "totalTime": 10},
            {"id": 33, "name": "单元测验", "dataType": None, "studyTime": None, "totalTime": 5},
            {"id": 34, "name": "已完成视频", "dataType": 3, "studyTime": 60, "totalTime": 60},
            {"id": 2, "name": "子章节", "childList": [
                {"id": 35, "name": "视频二", "dataType": 3, "studyTime": 12, "totalTime": 33}]}]}]})

    def view_file(self, session, prepared, query, **_):
        total = {"31": 50, "32": 10, "35": 33}[query["fileId"]]
        return make_response(prepared, {"status": 200, "rt": {"dataId": 880000 + int(query["fileId"]), "totalTime": total}})

    def study_record(self, session, prepared, query, **_):
        return make_response(prepared, {"status": 200, "rt": int(query["endWatchTime"])})

    # ---- AI course ------------------------------------------------------
    def ai(self, prepared, data):
        return make_response(prepared, {"code": 200, "message": "success", "data": data})

    def ai_basic(self, session, prepared, **_):
        return self.ai(prepared, {"courseName": "示例AI课", "cakeThemeList": [{"themeName": "主题一", "knowledgeList": [
            {"knowledgeId": 41, "knowledgeName": "知识点一", "studyProgress": 50},
            {"knowledgeId": 42, "knowledgeName": "知识点二", "studyProgress": 100}]}]})

    def ai_resources(self, session, prepared, **_):
        def resource(uid, name, kind, distribute, status=0, **extra):
            return {"studyStatus": status, "resourcesDetail": {"resourcesUid": uid, "resourcesName": name,
                    "resourcesType": kind, "resourcesDistributeType": distribute, **extra}}
        return self.ai(prepared, {"resourceList": [
            resource(501, "阅读材料", 2, 1),
            resource(502, "课件", 1, 4, resourcesUrl="https://file.example/502.pptx"),
            resource(503, "讲解视频", 1, 3, resourcesFileId=9001),
            resource(504, "课程视频", 2, 2, resourcesFileId=9002),
            resource(505, "其他资源", 3, 9),
            resource(506, "已学课件", 1, 4, status=1, resourcesUrl="https://file.example/506.pptx")]})

    def ai_empty(self, session, prepared, **_):
        return self.ai(prepared, {})

    def ai_video_time(self, session, prepared, secret, **_):
        return self.ai(prepared, [{"time": {9001: 12, 9002: 7}[secret["videoIdList"][0]]}])

    def ai_exam(self, session, prepared, secret, **_):
        knowledge = secret["knowledgeId"]
        sequence = self.mastery[knowledge]
        score = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return self.ai(prepared, {"paperId": 30 + knowledge, "examTestId": 40 + knowledge, "masteryScore": score})

    # ---- AI course exam -------------------------------------------------
    QUESTIONS = {
        91: (1, "<p>下列哪项正确？</p>", [(911, "甲", 0), (912, "乙", 1), (913, "丙", 0)], 1),
        92: (2, "<p>选择全部正确项</p>", [(921, "子", 1), (922, "丑", 0), (923, "寅", 1)], 0),
        93: (14, "<p>判断：一加一等于二</p>", [(931, "对", 1), (932, "错", 0)], 1),
    }

    def exam(self, prepared, data=None):
        return make_response(prepared, {"code": 0, "message": "成功", "data": data})

    def exam_ok(self, session, prepared, **_):
        return self.exam(prepared)

    def exam_sheet(self, session, prepared, **_):
        return self.exam(prepared, {"partSheetVos": [{"questionSheetVos": [
            {"questionId": 91, "version": 1}, {"questionId": 92, "version": 2}, {"questionId": 93}]}]})

    def exam_question(self, session, prepared, secret, **_):
        qtype, content, options, correct = self.QUESTIONS[secret["questionId"]]
        data = {"id": secret["questionId"], "questionType": qtype, "content": content,
                "optionVos": [{"id": oid, "content": text} for oid, text, _ in options]}
        if self.exam_submissions.get(secret["examTestId"]):
            data["userAnswerVo"] = [{"isCorrect": correct}]
            data["optionVos"] = [{"id": oid, "content": text, "isCorrect": flag} for oid, text, flag in options]
        return self.exam(prepared, data)

    def exam_submit(self, session, prepared, secret, **_):
        self.exam_submissions[secret["examTestId"]] = self.exam_submissions.get(secret["examTestId"], 0) + 1
        return self.exam(prepared)

    # ---- shared-course chapter tests and exams --------------------------
    def assessment(self, session, prepared, secret, **_):
        path = urlsplit(prepared.url).path.split("/gateway/t/v1/", 1)[1]
        paper = {"problemNum": 2, "workExamParts": [{"questionDtos": [
            {"eid": 801, "questionType": {"id": 1}, "name": "<p>题目一</p>",
             "questionOptions": [{"id": 8011, "content": "<p>甲</p>"}, {"id": 8012, "content": "乙"}]},
            {"eid": 802, "questionType": 2, "name": "题目二",
             "questionOptions": [{"id": 8021, "content": "丙"}, {"id": 8022, "content": "丁"},
                                 {"id": 8023, "content": "戊"}]}]}]}
        replies = {
            "exam/getLoginSchoolInfo": {"schoolId": 2001},
            "student/getStudentHomework": {"next": -1, "studentHomeworkList": [
                {"id": 601, "examId": 701, "examName": "第一章测验", "state": 1, "startTime": "", "endDate": "",
                 "meetCourseType": 0},
                {"id": 602, "examId": 702, "examName": "第二章测验", "state": 4}]},
            "student/getStudentFinalExam": [{"id": 611, "examId": 711, "examName": "期末考试", "state": 2,
                                             "startTime": "", "endDate": ""}],
            "answer/getSaveAnswerLockResult": {"state": 0},
            "student/doHomework": {"state": 1, "examBase": paper},
            "student/doExam": {"state": 1, "examBase": paper},
            "answer/submit": {"statu": 1, "score": 88},
        }
        if path == "answer/saveStudentAnswer":
            for answer in json.loads(secret["stuExamAnswer"]):
                self.saved_answers[(str(answer["stuExamId"]), str(answer["eid"]))] = answer["answer"]
            rt = {"statu": 1}
        elif path in ("answer/getStuAnswerInfoNew", "answer/getStuAnswerInfo"):
            rt = {question: {"answer": self.saved_answers.get((str(secret["stuExamId"]), question), "")}
                  for question in secret["questionIds"].split(",")}
        elif path in replies:
            rt = replies[path]
        else:
            self.unhandled.append({"assessment": path})
            return make_response(prepared, {"status": "404"}, status=404)
        return make_response(prepared, {"status": "200", "code": 0, "rt": rt})

    # ---- models ---------------------------------------------------------
    @staticmethod
    def answer_text(prompt):
        if '"option_ids"' in prompt:
            question = json.loads(prompt.split("\n", 1)[1])
            ids = [option["id"] for option in question["options"]][:2 if question["type"] == 2 else 1]
            return json.dumps({"option_ids": ids})
        choices = json.loads(re.search(r"```choices\n(.*?)\n```", prompt, re.S)[1])
        picked = choices[:2] if "多选题" in prompt else choices[:1]
        return ("我选择这个答案。\n```answer\n" + json.dumps(picked, ensure_ascii=False) + "\n```\n依据：示例资料。")

    def completion(self, prepared, prompt, stream, *, plain_prefix, stream_prefix):
        text = self.answer_text(prompt)
        if not stream:
            payload = json.dumps({"choices": [{"message": {"content": text}}]}, ensure_ascii=False)
            return make_response(prepared, text=plain_prefix + payload)
        pieces = [text[:7], text[7:30], text[30:]]
        lines = [stream_prefix + json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)
                 for piece in pieces] + [stream_prefix + "[DONE]"]
        return make_response(prepared, text="\n\n".join(lines) + "\n")

    def platform_ai(self, session, prepared, body, **_):
        return self.completion(prepared, body["messageList"][0]["content"], body["stream"],
                               plain_prefix="data:", stream_prefix="data:")

    def openai(self, session, prepared, body, **_):
        return self.completion(prepared, body["messages"][0]["content"], body["stream"],
                               plain_prefix="", stream_prefix="data: ")


STUDY = "studyservice-api.zhihuishu.com"
AI_HOST = "kg-ai-run.zhihuishu.com"
EXAM_HOST = "studentexamtest.zhihuishu.com"
ROUTES = {
    ("passport.zhihuishu.com", "/qrCodeLogin/getLoginQrImg"): Platform.qr_image,
    ("passport.zhihuishu.com", "/qrCodeLogin/getLoginQrInfo"): Platform.qr_info,
    ("passport.zhihuishu.com", "/login"): Platform.login_page,
    ("passport.zhihuishu.com", "/cas/login"): Platform.cas,
    ("onlineservice-api.zhihuishu.com", "/gateway/t/v1/student/course/share/queryShareCourseInfo"): Platform.share_list,
    ("onlineservice-api.zhihuishu.com", "/gateway/t/v1/student/queryStudentAICourseList"): Platform.ai_list,
    ("hikeservice.zhihuishu.com", "/student/course/aided/getMyCourseList"): Platform.hike_list,
    (STUDY, "/login/gologin"): Platform.ok,
    (STUDY, "/gateway/t/v1/learning/queryCourse"): Platform.query_course,
    (STUDY, "/gateway/t/v1/learning/videolist"): Platform.video_list,
    (STUDY, "/gateway/t/v1/learning/queryStuyInfo"): Platform.study_info,
    (STUDY, "/gateway/t/v1/learning/queryUserRecruitIdLastVideoId"): Platform.empty,
    (STUDY, "/gateway/t/v1/learning/prelearningNote"): Platform.prelearning,
    (STUDY, "/gateway/t/v1/popupAnswer/loadVideoPointerInfo"): Platform.pointers,
    (STUDY, "/gateway/t/v1/popupAnswer/lessonPopupExam"): Platform.popup,
    (STUDY, "/gateway/t/v1/popupAnswer/saveLessonPopupExamSaveAnswer"): Platform.empty,
    (STUDY, "/gateway/t/v1/course/threeDimensionalCourseWare"): Platform.empty,
    (STUDY, "/gateway/t/v1/learning/saveDatabaseIntervalTimeV2"): Platform.report,
    ("newbase.zhihuishu.com", "/video/initVideo"): Platform.init_video,
    ("studyresources.zhihuishu.com", "/studyResources/stuResouce/queryResourceMenuTree"): Platform.menu_tree,
    ("studyresources.zhihuishu.com", "/studyResources/stuResouce/stuViewFile"): Platform.view_file,
    ("hike-teaching.zhihuishu.com", "/stuStudy/saveStuStudyRecord"): Platform.study_record,
    (AI_HOST, "/run/gateway/t/stu/knowledge-study/course-basic"): Platform.ai_basic,
    (AI_HOST, "/run/gateway/t/stu/resources/list-knowledge-resource"): Platform.ai_resources,
    (AI_HOST, "/run/gateway/t/stu/studyRecord/completed"): Platform.ai_empty,
    (AI_HOST, "/run/gateway/t/stu/resources-lab/get-video-time"): Platform.ai_video_time,
    (AI_HOST, "/run/gateway/t/stu/studyRecord/report"): Platform.ai_empty,
    (AI_HOST, "/run/gateway/t/stu/exam/questions-paper"): Platform.ai_exam,
    (EXAM_HOST, "/gateway/t/v1/exam/user/openExam"): Platform.exam_ok,
    (EXAM_HOST, "/gateway/t/v1/exam/user/updateUserUsedTime"): Platform.exam_ok,
    (EXAM_HOST, "/gateway/t/v1/exam/user/getExamSheetInfo"): Platform.exam_sheet,
    (EXAM_HOST, "/gateway/t/v1/question/getExamQuestionInfo"): Platform.exam_question,
    (EXAM_HOST, "/gateway/t/v1/answer/saveAnswer"): Platform.exam_ok,
    (EXAM_HOST, "/gateway/t/v1/exam/user/submit"): Platform.exam_submit,
    ("ai-knowledge-map-platform.zhihuishu.com", "/knowledgemap/gateway/t/qa/platform/stream"): Platform.platform_ai,
}
