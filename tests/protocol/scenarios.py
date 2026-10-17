"""Adapter-neutral protocol scenarios. The legacy recorder and new tests both read these."""

import copy

from .fake_platform import AI_CLASS_ID, AI_COURSE_ID, CASLOGC, HIKE_ID, SHARED_ID

COOKIES = [{"name": "CASLOGC", "value": CASLOGC, "domain": ".zhihuishu.com", "path": "/"},
           {"name": "CASTGC", "value": "TGT-fake", "domain": ".zhihuishu.com", "path": "/"}]
SETTINGS = {"speed": None, "limit": 0, "threshold": 0.91}
AI_BASE = {"enabled": True, "use_zhidao_ai": True, "use_stream": False, "no_exam": False, "max_requests": 50,
           "openai": {"api_base": "https://api.openai.com/v1", "api_key": "", "model_name": ""},
           "ppt_processing": {"provide_to_ai": False, "moonShot": {
               "base_url": "https://api.moonshot.cn/v1", "api_key": "", "delete_after_convert": True}}}
AI_OPENAI_STREAM = {**copy.deepcopy(AI_BASE), "use_zhidao_ai": False, "use_stream": True,
                    "openai": {"api_base": "https://llm.example/v1", "api_key": "sk-fake-key", "model_name": "test-model"}}

SHARED = ("run", "zhidao", SHARED_ID)
HIKE = ("run", "hike", HIKE_ID)
AI = ("run", "ai", AI_COURSE_ID, AI_CLASS_ID)


def scenario(ops, *, seed=0, settings=None, ai=None, cookies=COOKIES, platform=None, deadline=None):
    """``deadline`` is in fake seconds from the start of the first operation."""
    return {"ops": [list(op) for op in ops], "seed": seed, "settings": {**SETTINGS, **(settings or {})},
            "ai": copy.deepcopy(ai or AI_BASE), "cookies": copy.deepcopy(cookies),
            "platform": platform or {}, "deadline": deadline}


SCENARIOS = {
    "qr_login": scenario([("login",)], cookies=None, platform={"qr_expire_first": True}),
    "course_lists": scenario([("list", "zhidao"), ("list", "hike"), ("list", "ai")]),
    # Seed 0 makes the long video take one of the random one-minute pauses.
    "shared_course": scenario([SHARED]),
    "shared_course_fast": scenario([SHARED], settings={"speed": 3, "threshold": 0.5}),
    "shared_course_rewatch": scenario([SHARED], settings={"speed": 16, "threshold": 1.2}),
    "shared_course_captcha": scenario([SHARED], platform={"captcha_on_report": True}),
    "shared_course_stop": scenario([SHARED], deadline=100),
    "hike_course": scenario([HIKE]),
    "hike_course_fast": scenario([HIKE], settings={"speed": 4, "threshold": 0.5}),
    "hike_course_stop": scenario([HIKE], deadline=20),
    "ai_course_no_exam": scenario([AI], ai={**AI_BASE, "no_exam": True}),
    "ai_course_exam_platform": scenario([AI]),
    "ai_course_exam_openai_stream": scenario([AI], ai=AI_OPENAI_STREAM),
    "assessments": scenario([("assessments", SHARED_ID, ["chapter:601", "exam:611"])]),
}
