"""Model providers: the platform's built-in AI and OpenAI-compatible chat services.

Every HTTP attempt, including retries, consumes one unit of the task's request
budget before it is sent; running out raises :class:`AIBudgetExceeded`, which
is a BaseException so no broad error handler can turn it into a retry.
"""

import ast
import hashlib
import json
import logging
import random
import re
import string
import time
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import requests

log = logging.getLogger("killzhs.ai")

PLATFORM_URL = "https://ai-knowledge-map-platform.zhihuishu.com/knowledgemap/gateway/t/qa/platform/stream"
PLATFORM_MODEL = "moonshot-v1-32k"
PLATFORM_SIGN_PREFIX = "8ZflKEagfL"
TOKEN_LIMIT = 27_900


class AIBudgetExceeded(BaseException):
    """The per-task generation request limit was reached."""


class RequestBudget:
    def __init__(self, limit, on_use=lambda used: None):
        self.limit, self.used, self.on_use = limit, 0, on_use

    @property
    def remaining(self):
        return self.limit - self.used

    def consume(self):
        if self.used >= self.limit:
            raise AIBudgetExceeded()
        self.used += 1
        self.on_use(self.used)


@lru_cache(maxsize=1)
def encoder():
    import tiktoken
    return tiktoken.encoding_for_model("gpt-4")


class ChatModel:
    """Shared retry loop and server-sent-event parsing."""

    def __init__(self, *, stream, budget, control=None):
        self.stream, self.budget, self.control = stream, budget, control

    def complete(self, prompt, *, aim_start="```answer", aim_end="```", max_retries=3, retry_delay=1.0):
        request = self.prepare(prompt)
        for attempt in range(max_retries):
            try:
                if self.control is not None:
                    self.control.check()
                self.budget.consume()
                response = request()
                try:
                    response.raise_for_status()
                    if self.stream:
                        return self.read_stream(response, aim_start, aim_end, control=self.control)
                    return self.read(response)["choices"][0]["message"]["content"]
                finally:
                    # An answer block can finish before the SSE body does. Always
                    # release that response, including parsing errors and retries.
                    response.close()
            except requests.exceptions.RequestException as error:
                log.error("模型请求第 %d 次失败：%s", attempt + 1, error)
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    log.error("模型请求已全部失败，放弃本题。")
                    raise
        raise RuntimeError("Unexpected end of completion retries")

    @staticmethod
    def read_stream(response, aim_start, aim_end, control=None):
        """Collect deltas until the answer block is complete; return whatever arrived."""
        text = ""
        pattern = re.compile(f"{re.escape(aim_start)}(.*?){re.escape(aim_end)}", re.DOTALL)
        for line in response.iter_lines():
            if control is not None:
                control.check()
            if not line:
                continue
            try:
                content = json.loads(line.decode("utf-8")[5:]).get("choices", [{}])[0].get("delta", {}).get("content", "")
            except json.JSONDecodeError:
                continue
            if content:
                text += content
                if pattern.search(text):
                    return text
        return text


class PlatformAI(ChatModel):
    """The platform's assistant, reached with the logged-in course session."""

    def __init__(self, session, **options):
        super().__init__(**options)
        self.session = session

    def prepare(self, prompt):
        body = {"messageList": [{"role": "user", "content": prompt}], "modelCode": PLATFORM_MODEL, "stream": self.stream}
        url, body = sign(PLATFORM_URL, body)
        return lambda: self.session.post(url, json=body, timeout=30, stream=self.stream)

    @staticmethod
    def read(response):
        return json.loads(response.text[5:])  # Strip the leading "data:".


class OpenAICompatible(ChatModel):
    def __init__(self, base_url, api_key, model, *, session_factory=requests.Session, **options):
        super().__init__(**options)
        self.base_url, self.api_key, self.model = base_url, api_key, model
        self.session_factory = session_factory

    def prepare(self, prompt):
        root = self.base_url.rstrip("/")
        if not urlparse(root).path:
            root += "/v1"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        body = {"messages": [{"role": "user", "content": prompt}], "model": self.model, "stream": self.stream}

        def post():
            # A fresh session per attempt, exactly like ``requests.post``.
            with self.session_factory() as session:
                return session.post(f"{root}/chat/completions", headers=headers, json=body, timeout=30,
                                    stream=self.stream)
        return post

    @staticmethod
    def read(response):
        return response.json()


def sign(url, body):
    """The platform assistant signs a fixed rendering of the request with MD5."""
    scheme, netloc, path, query, fragment = urlsplit(url)
    params = dict(parse_qsl(query))
    if "sessionNid" not in body:
        characters = string.ascii_lowercase + string.digits
        body["sessionNid"] = "chatcmpl-" + "".join(random.choice(characters) for _ in range(24))
    rendered = json.dumps({"messageList": "[object Object]", "modelCode": body.get("modelCode", ""),
                           "sessionNid": body.get("sessionNid", ""), "stream": body.get("stream", False)},
                          separators=(",", ":")).replace('"true"', "true").replace('"false"', "false")
    params["sign"] = hashlib.md5((PLATFORM_SIGN_PREFIX + rendered).encode("utf-8")).hexdigest()
    return urlunsplit((scheme, netloc, path, urlencode(params), fragment)), body


# ---- AI-course exam prompts -----------------------------------------------

ANSWER_EXAMPLE = (
    "        ```answer\n"
    "        [{\"id\": 652308395, \"content\": \"<p>仅涉及心、肺、肝、肾及脑相关疾病</p>\"}]\n"
    "        ```\n"
    "        答案必须为满足格式的json字符串(列表，单选也要是列表)，否则视为无效答案，不能得分。"
    "在这个markdown代码块（answer）外，你需要解释为什么你认为这个答案是正确的，并且标注出你选择这个答案的依据，"
    "这些依据必须有一定的权威性。（我提供给你的参考资料绝对权威可信）\n"
    "        ")
QUESTION_KINDS = {1: "单选题", 2: "多选题", 14: "判断题"}


def exam_prompt(kind, question, choices, materials, extra):
    """The AI-course exam prompt, kept character for character."""
    references = "参考资料：\n".join(f"```{item.get('name', '')}\n{item.get('content', '')}\n```" for item in materials)
    background = f"假设你是一名学生，正在学习《{extra.get('courseName', '未知课程')}》。需要严格按照考试要求完成一道题目，否则无法及格。\n"
    theme = f"现在，你学习到了{extra.get('theme', '未知主题')}。\n" if "theme" in extra else ""
    point = f"本次考察知识点为{extra.get('knowledgePoint', '未知知识点')}。\n" if "knowledgePoint" in extra else ""
    scope = "最合适的答案" if kind == "单选题" else "所有正确的答案"
    requirement = f"本题为{kind}，请从选项中选择{scope}，回答放到markdown代码块中，例如：\n\n" + ANSWER_EXAMPLE
    return (references + background + theme + point + requirement + f"现在，请听题：\n\n{question}\n\n"
            + f"选项如下：```choices\n{json.dumps(choices, ensure_ascii=False, indent=4)}\n```\n")


def generate_answer(model, prompt):
    """Ask for an ``answer`` block and return the chosen option IDs."""
    tokens = encoder().encode(prompt)
    if len(tokens) > TOKEN_LIMIT:
        tokens = tokens[-TOKEN_LIMIT:]
        prompt = encoder().decode(tokens)
        log.warning("提示过长，已截取最后 %d 个 token。", len(tokens))
    completion = model.complete(prompt)
    block = re.search(r"```answer\n(.*?)\n```", completion, re.DOTALL)
    if block is None:
        raise ValueError("Answer block not found in completion")
    try:
        answers = json.loads(block[1])
    except json.JSONDecodeError:
        try:
            answers = ast.literal_eval(block[1])
        except (ValueError, SyntaxError) as error:
            raise ValueError(f"Invalid answer format: {block[1]}") from error
    if not isinstance(answers, list):
        raise ValueError(f"Answer should be a list, got: {type(answers)}")
    if any(not isinstance(item, dict) or "id" not in item or "content" not in item for item in answers):
        raise ValueError(f"Invalid answer format: {answers}")
    return [item["id"] for item in answers]
