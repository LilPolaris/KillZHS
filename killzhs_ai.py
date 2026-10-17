"""Model providers and course-material conversion, shared by assessment flows."""

import ast
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from random import choice as random_choice
import re
import string
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from openai import OpenAI
import requests
import tiktoken
from logger import logger
from utils import getRealPath

class Openai:
    def __init__(self, baseUrl: str = "", apiKey: str = "", modelName: str = "", useZhidao: bool = False, zhiDaosession: requests.Session = None, stream: bool = False, extra: dict = {}):
        self.baseUrl = baseUrl
        self.apiKey = apiKey
        self.modelName = modelName
        self.extra = extra

        self.stream = stream

        self.session = zhiDaosession if zhiDaosession is not None else requests.Session()
        self.useZhidao = useZhidao

        self.encoder = tiktoken.encoding_for_model("gpt-4")

        self.prefix = "8ZflKEagfL"

    def __parseStream(self, response: requests.Response, aim_start: str, aim_end: str) -> str:
        """解析 stream 响应"""
        cache = ""
        for line in response.iter_lines():
            if not line:
                continue

            try:
                json_data = json.loads(line.decode('utf-8')[5:])
                content = json_data.get("choices", [{}])[0].get(
                    "delta", {}).get("content", "")
                if content:
                    cache += content
                    # 使用正则寻找答案
                    match = re.search(
                        f"{re.escape(aim_start)}(.*?){re.escape(aim_end)}", cache, re.DOTALL)
                    if match:
                        return cache
            except json.JSONDecodeError:
                continue

    def openaiCompletion(self, prompt: str, aimStart: str = "```answer", aimEnd: str = "```", max_retries: int = 3, retry_delay: float = 1.0) -> str:
        api_root = self.baseUrl.rstrip("/")
        if not urlparse(api_root).path:
            api_root += "/v1"
        url = f"{api_root}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.apiKey}",
        }

        body = {
            "messages": [{"role": "user", "content": prompt}],
            "model": self.modelName,
            "stream": self.stream,
        }

        for attempt in range(max_retries):
            try:
                response = requests.post(
                    url, headers=headers, json=body, timeout=30, stream=self.stream)
                response.raise_for_status()

                if self.stream:
                    result = self.__parseStream(response, aimStart, aimEnd)
                else:
                    result = response.json()

                return result["choices"][0]["message"]["content"] if isinstance(result, dict) else result
            except requests.exceptions.RequestException as e:
                logger.error(
                    f"Completion attempt {attempt + 1} failed: {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error(
                        "All completion attempts failed, giving up...")
                    raise

        # This line should never be reached, but it's here for completeness
        raise Exception("Unexpected error in completion method")

    def zhiDaoCompletion(self, prompt: str, aimStart: str = "```answer", aimEnd: str = "```", max_retries: int = 3, retry_delay: float = 1.0) -> str:
        base_url = "https://ai-knowledge-map-platform.zhihuishu.com/knowledgemap/gateway/t/qa/platform/stream"

        body = {
            "messageList": [{"role": "user", "content": prompt}],
            "modelCode": "moonshot-v1-32k",
            "stream": self.stream,
        }

        url, body = self.__zhidaoSign(base_url, body)

        for attempt in range(max_retries):
            try:
                response = self.session.post(
                    url, json=body, timeout=30, stream=self.stream)
                response.raise_for_status()

                if self.stream:
                    result = self.__parseStream(response, aimStart, aimEnd)
                else:
                    result = json.loads(response.text[5:])

                return result["choices"][0]["message"]["content"] if isinstance(result, dict) else result
            except requests.exceptions.RequestException as e:
                logger.error(
                    f"Completion attempt {attempt + 1} failed: {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error(
                        "All completion attempts failed, giving up...")
                    raise

        # This line should never be reached, but it's here for completeness
        raise Exception("Unexpected error in completion method")

    def __generate_random_session_nid(self):
        chars = string.ascii_lowercase + string.digits
        return "chatcmpl-" + ''.join(random_choice(chars) for _ in range(24))

    def __generate_signature(self, input_string: str) -> str:
        """生成 MD5 签名"""
        return hashlib.md5((self.prefix + input_string).encode('utf-8')).hexdigest()

    def __build_input_string(self, data: dict) -> str:
        """构建输入字符串"""
        json_data = {
            "messageList": "[object Object]",
            "modelCode": data.get("modelCode", ""),
            "sessionNid": data.get("sessionNid", ""),
            "stream": data.get("stream", False)
        }
        return json.dumps(json_data, separators=(',', ':')) \
            .replace('"true"', 'true') \
            .replace('"false"', 'false')

    def __zhidaoSign(self, url: str, data: dict) -> tuple[str, dict]:
        """处理 URL 和 body 数据，返回带签名的 URL 和更新后的 body"""
        # 解析 URL
        scheme, netloc, path, query_string, fragment = urlsplit(url)

        # 解析查询参数
        query_params = dict(parse_qsl(query_string))

        # 如果 sessionNid 不在 data 中，生成一个随机的
        if "sessionNid" not in data:
            data["sessionNid"] = self.__generate_random_session_nid()

        # 生成签名
        input_string = self.__build_input_string(data)
        signature = self.__generate_signature(input_string)

        # 添加签名到查询参数
        query_params['sign'] = signature

        # 重新构建查询字符串
        new_query_string = urlencode(query_params)

        # 重新构建 URL
        signed_url = urlunsplit(
            (scheme, netloc, path, new_query_string, fragment))

        return signed_url, data

    def _baseTemplate(self, question, choices, referenceMaterials: list, answer_type: str) -> str:
        if len(referenceMaterials) == 0:
            reserenceMaterial = ""
        else:
            reserenceMaterial = "参考资料：\n".join(
                [f"```{material.get('name', '')}\n{material.get('content', '')}\n```" for material in referenceMaterials])

        aiBackground = f"假设你是一名学生，正在学习《{self.extra.get('courseName', '未知课程')}》。需要严格按照考试要求完成一道题目，否则无法及格。\n"
        theme = f"现在，你学习到了{self.extra.get('theme', '未知主题')}。\n" if "theme" in self.extra else ""
        knowledgePoint = f"本次考察知识点为{self.extra.get('knowledgePoint', '未知知识点')}。\n" if "knowledgePoint" in self.extra else ""

        answerRequirment = f"""本题为{answer_type}，请从选项中选择{'最合适的答案' if answer_type == '单选题' else '所有正确的答案'}，回答放到markdown代码块中，例如：

        ```answer
        [{{"id": 652308395, "content": "<p>仅涉及心、肺、肝、肾及脑相关疾病</p>"}}]
        ```
        答案必须为满足格式的json字符串(列表，单选也要是列表)，否则视为无效答案，不能得分。在这个markdown代码块（answer）外，你需要解释为什么你认为这个答案是正确的，并且标注出你选择这个答案的依据，这些依据必须有一定的权威性。（我提供给你的参考资料绝对权威可信）
        """
        questionContent = f"现在，请听题：\n\n{question}\n\n"
        choicesContent = f"选项如下：```choices\n{json.dumps(choices, ensure_ascii=False, indent=4)}\n```\n"
        return reserenceMaterial + aiBackground + theme + knowledgePoint + answerRequirment + questionContent + choicesContent

    def singleChoiceTemplate(self, question, choices, referenceMaterials: list = []) -> str:
        return self._baseTemplate(question, choices, referenceMaterials, "单选题")

    def multipleChoiceTemplate(self, question, choices, referenceMaterials: list = []) -> str:
        return self._baseTemplate(question, choices, referenceMaterials, "多选题")

    def judgementTemplate(self, question, choices, referenceMaterials: list = []) -> str:
        return self._baseTemplate(question, choices, referenceMaterials, "判断题")

    def generateAnswer(self, prompt: str) -> list:
        tokens = self.encoder.encode(prompt)

        # 如果大于32ktoken，取后32ktoken
        if len(tokens) > 27.900 * 1000:
            tokens = tokens[-27900:]

            prompt = self.encoder.decode(tokens)
            logger.warning(
                f"Prompt is too long, truncated to {len(tokens)} tokens")
        if self.useZhidao:
            aicompletion = self.zhiDaoCompletion(prompt)
        else:
            aicompletion = self.openaiCompletion(prompt)

        # 寻找```answer \n ... \n```块
        answer_block = re.search(
            r"```answer\n(.*?)\n```", aicompletion, re.DOTALL)
        if answer_block is None:
            raise ValueError("Answer block not found in completion")

        # 解析答案
        answer_str = answer_block.group(1)
        try:
            # 首先尝试用 json.loads
            answer_list = json.loads(answer_str)
        except json.JSONDecodeError:
            try:
                # 如果 json.loads 失败，尝试用 ast.literal_eval
                answer_list = ast.literal_eval(answer_str)
            except (ValueError, SyntaxError):
                raise ValueError(f"Invalid answer format: {answer_str}")

        if not isinstance(answer_list, list):
            raise ValueError(
                f"Answer should be a list, got: {type(answer_list)}")

        answers = []
        # 验证答案格式
        for answer in answer_list:
            if not isinstance(answer, dict) or "id" not in answer or "content" not in answer:
                raise ValueError(f"Invalid answer format: {answer}")
            answers.append(answer["id"])

        return answers


class PptToTxt:
    def __init__(self, moonShotKey,baseUrl="https://api.moonshot.cn/v1", max_file_size_mb=100, max_cache_files=500, max_cache_size_gb=8, delete_immediately=False):
        self.__moonShotKey = moonShotKey
        self.__session = requests.Session()
        self.__download_path = getRealPath("AiDownloadCache")
        self.__client = OpenAI(
            api_key=self.__moonShotKey,
            base_url=baseUrl,
        )
        self.__max_file_size = max_file_size_mb * 1024 * 1024  # Convert to bytes
        self.__max_cache_files = max_cache_files
        self.__max_cache_size = max_cache_size_gb * \
            1024 * 1024 * 1024  # Convert to bytes
        self.__delete_immediately = delete_immediately
        self.__file_cache = {}
        self.__initialize_cache()

    def __initialize_cache(self):
        # Manage only uploads created by this instance, never unrelated user files.
        self.__file_cache = {}

    def parseTxt(self, url: str) -> str:
        try:
            local_path = self.__getFilePath(url)
            file_size = os.path.getsize(local_path)
            if file_size > self.__max_file_size:
                raise ValueError(
                    f"File size ({file_size} bytes) exceeds the maximum allowed size ({self.__max_file_size} bytes)")

            file_id = self.__uploadFile(local_path)
            text_content = self.__extractText(file_id)
            self.__manage_cache()
            return text_content
        except Exception as e:
            logger.error(f"Error processing file from URL {url}: {str(e)}")
            return ""

    def __getFilePath(self, url: str) -> str:
        suffix = Path(urlparse(url).path).suffix.lower()
        suffix = suffix if suffix in (".pdf", ".ppt", ".pptx", ".doc", ".docx", ".txt") else ".bin"
        file_path = hashlib.sha256(url.encode("utf-8")).hexdigest() + suffix
        local_path = os.path.join(self.__download_path, file_path)

        if os.path.exists(local_path):
            logger.info(f"File already exists: {local_path}")
            return local_path

        return self.__downloadFile(url)

    def __downloadFile(self, url: str) -> str:
        response = self.__session.get(url, stream=True, timeout=30)
        if response.status_code == 200:
            suffix = Path(urlparse(url).path).suffix.lower()
            suffix = suffix if suffix in (".pdf", ".ppt", ".pptx", ".doc", ".docx", ".txt") else ".bin"
            file_path = hashlib.sha256(url.encode("utf-8")).hexdigest() + suffix
            local_path = os.path.join(self.__download_path, file_path)

            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            with open(local_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info(f"File downloaded: {local_path}")
            return local_path
        else:
            raise Exception(f"Failed to download file: {response.status_code}")

    def __uploadFile(self, filePath: str) -> str:
        filename = os.path.basename(filePath)
        file_size = os.path.getsize(filePath)

        if filename in self.__file_cache and self.__file_cache[filename]['size'] == file_size:
            logger.info(
                f"File {filename} already exists on the server. Using existing file.")
            return self.__file_cache[filename]['id']

        file_object = self.__client.files.create(
            file=Path(filePath), purpose="file-extract")
        self.__file_cache[filename] = {
            'id': file_object.id,
            'size': file_size,
            'created_at': datetime.now().timestamp()
        }
        return file_object.id

    def __extractText(self, file_id: str) -> str:
        try:
            file_content = self.__client.files.content(file_id=file_id).text

            try:
                json_content = json.loads(file_content)
                return json_content.get("content", file_content)
            except json.JSONDecodeError:
                return file_content
        finally:
            if self.__delete_immediately:
                self.__deleteFile(file_id)

    def __deleteFile(self, file_id: str):
        try:
            self.__client.files.delete(file_id=file_id)
            logger.info(
                f"File with ID {file_id} deleted from MoonShot remote server")
            # Remove from cache
            self.__file_cache = {
                k: v for k, v in self.__file_cache.items() if v['id'] != file_id}
        except Exception as e:
            logger.error(f"Error deleting file with ID {file_id}: {str(e)}")

    def __manage_cache(self):
        total_size = sum(file['size'] for file in self.__file_cache.values())
        total_files = len(self.__file_cache)

        while (total_files > self.__max_cache_files or total_size > self.__max_cache_size) and self.__file_cache:
            oldest_file = min(self.__file_cache.items(),
                              key=lambda x: x[1]['created_at'])
            self.__deleteFile(oldest_file[1]['id'])
            total_size -= oldest_file[1]['size']
            total_files -= 1
