"""AI-course exam workflow, separate from video playback and model providers."""
from __future__ import annotations

import json
import os
from random import randint, sample
import threading
import time
from typing import TYPE_CHECKING
import requests
from killzhs_ai import Openai
from logger import logger
from utils import getRealPath, progressBar
from zd_utils import EXAM_KEY

if TYPE_CHECKING:
    from fucker import Fucker

class ExamCtx:
    def __init__(self, fucker: Fucker, courseId: int, knowledgeId: int, examTestId: int, examPaperId: int, progress_view: bool = True, aiConfig: dict = {}, opExtra: dict = {}):
        self.fucker = fucker
        self.courseId = courseId
        self.examTestId = examTestId
        self.examPaperId = examPaperId
        self.knowledgeId = knowledgeId
        self.opExtra = opExtra
        self.aiConfig = aiConfig
        self.progress_view = progress_view

        self.answerCache = {}
        self.allAnswerCache = {} # 存储同课程所有题目的答案
        self.sheetContent = None
        self.timeUpdateIndex = 0
        self.examStopped = False

        if aiConfig.get("enabled", False) and not aiConfig.get("use_zhidao_ai", False):
            opConf: dict = aiConfig.get("openai", {})
            self.op = Openai(
                baseUrl=opConf.get("api_base", "https://api.openai.com/v1"),
                apiKey=opConf.get("api_key", ""),
                modelName=opConf.get("model_name", "davinci"),
                stream=aiConfig.get("use_stream", False),
                extra=opExtra,
            )

        if aiConfig.get("enabled", False) and aiConfig.get("use_zhidao_ai", False):
            session = requests.Session()
            session.cookies.update(fucker.session.cookies)
            session.headers.update(fucker.session.headers)
            session.proxies.update(fucker.proxies)
            self.op = Openai(
                useZhidao=True,
                zhiDaosession=session,
                stream=aiConfig.get("use_stream", False),
                extra=opExtra,
            )

    def getAnswerpath(self, examTestId):
        answerpath = getRealPath(
            f"aiexamAnswer/{self.courseId}/{examTestId}.json")

        # 确保目录存在
        os.makedirs(os.path.dirname(answerpath), exist_ok=True)

        # 如果文件不存在，创建一个包含空字典的 JSON 文件
        if not os.path.exists(answerpath):
            with open(answerpath, "w", encoding="utf-8") as f:
                json.dump({}, f)

        return answerpath
    
    def getAllAnswerpath(self): # 同课程所有题目的答案
        answerpath = getRealPath(
            f"aiexamAnswer/{self.courseId}/data.json")

        # 确保目录存在
        os.makedirs(os.path.dirname(answerpath), exist_ok=True)

        # 如果文件不存在，创建一个包含空字典的 JSON 文件
        if not os.path.exists(answerpath):
            with open(answerpath, "w", encoding="utf-8") as f:
                json.dump({}, f)

        return answerpath

    def readAnswerCache(self, examTestId):
        Allanwerpath = self.getAllAnswerpath()
        path = getRealPath(f"aiexamAnswer/{self.courseId}/")
        files = os.listdir(path)
        Allcache = {}
        for file in files:
            if file == "data.json":
                continue
            file_path = os.path.join(path, file)
            with open(file_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
                Allcache.update(cache)
        
        self.allAnswerCache = {}
        for key, value in Allcache.items():
            if '_' in key:
                questionId, version = key.split('_')
                self.allAnswerCache[f"{questionId}_{version}"] = value
            else:
                self.allAnswerCache[key] = value
                # 为没有版本号的答案添加默认版本
                value['version'] = 1


        answerpath = self.getAnswerpath(examTestId)
        with open(answerpath, "r", encoding="utf-8") as f:
            cache = json.load(f)

        # 转换缓存格式以支持版本号
        self.answerCache = {}
        for key, value in cache.items():
            if '_' in key:
                questionId, version = key.split('_')
                self.answerCache[f"{questionId}_{version}"] = value
            else:
                self.answerCache[key] = value
                # 为没有版本号的答案添加默认版本
                value['version'] = 1

        return self.answerCache, self.allAnswerCache

    def writeAnswerCacheToDisk(self):
        answerpath = self.getAnswerpath(self.examTestId)
        with open(answerpath, "w", encoding="utf-8") as f:
            json.dump(self.answerCache, f, ensure_ascii=False, indent=4)

        allAnswerpath = self.getAllAnswerpath()
        with open(allAnswerpath, "w", encoding="utf-8") as f:
            json.dump(self.allAnswerCache, f, ensure_ascii=False, indent=4)

    def getAnswer(self, questionId: int, version: int = 1) -> dict | None:
        key = str(questionId) if version == 1 else f"{questionId}_{version}"
        result = self.allAnswerCache.get(key) # 从同课程所有题目的答案中获取
        return result

    def setAnswer(self, questionId: int, version: int, answer_data: dict):
        self.answerCache[str(questionId) if version == 1 else f"{questionId}_{version}"] = {
            "version": version,
            "question": answer_data.get("question", ""),
            "answer": answer_data.get("answer", ""),
            "answer_content": answer_data.get("answer_content", ""),
            "questionDict": answer_data.get("questionDict", {})
        }
        self.allAnswerCache[str(questionId) if version == 1 else f"{questionId}_{version}"] = {
            "version": version,
            "question": answer_data.get("question", ""),
            "answer": answer_data.get("answer", ""),
            "answer_content": answer_data.get("answer_content", ""),
            "questionDict": answer_data.get("questionDict", {})
        }

        self.writeAnswerCacheToDisk()

    def openExam(self, triedTimes: int = 0):
        url = f"https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/openExam"

        data = {
            "examTestId": self.examTestId,
            "examPaperId": self.examPaperId,
            "courseId": self.courseId,
        }
        try:
            ret = self.fucker.zhidaoAiExamQuery(
                url, data, ok_code=0, key=EXAM_KEY, method="POST")

        except Exception as e:
            if triedTimes < 3:
                logger.error(
                    f"openExam failed, retrying... {triedTimes} times")
                return self.openExam(triedTimes + 1)
            else:
                logger.error(f"openExam failed, retried 3 times, giving up...")
                raise e
        # 启动定时更新时间的线程
        threading.Thread(target=self.updateExamCostTime,
                         args=(10,)).start()
        return True

    def updateExamCostTime(self, heartbeatTime: int = 10):
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/updateUserUsedTime"

        data = {
            "examTestId": self.examTestId,
            "examPaperId": self.examPaperId,
            "heartbeatTime": heartbeatTime
        }

        while not self.examStopped:
            try:
                ret = self.fucker.zhidaoAiExamQuery(
                    url, data, ok_code=0, key=EXAM_KEY, method="POST")
            except Exception as e:
                logger.error(f"updateExamCostTime failed: {e}")
            finally:
                self.timeUpdateIndex += 1
                if self.timeUpdateIndex % 10 == 0:
                    logger.info(
                        f"Exam {self.examTestId} cost time: {self.timeUpdateIndex * heartbeatTime}s")

                time.sleep(heartbeatTime)

    def getSheetContent(self, triedTimes: int = 0) -> list:
        if self.sheetContent is not None:
            return self.sheetContent
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/getExamSheetInfo"

        data = {
            "examTestId": self.examTestId,
            "examPaperId":  self.examPaperId
        }

        try:
            ret = self.fucker.zhidaoAiExamQuery(
                url, data, ok_code=0, key=EXAM_KEY, method="GET")
            self.sheetContent = ret["data"]["partSheetVos"][0]["questionSheetVos"]
        except Exception as e:
            logger.error(f"getSheetContent failed: {e}")

            if triedTimes < 3:
                logger.error(
                    f"getSheetContent failed, retrying... {triedTimes} times")
                return self.getSheetContent(triedTimes + 1)
            else:
                logger.error(
                    f"getSheetContent failed, retried 3 times, giving up...")
                raise e

        return self.sheetContent

    def getQuestionContent(self, questionId: int, version: int, triedTimes: int = 0) -> dict:
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/question/getExamQuestionInfo"
        data = {
            "examTestId": self.examTestId,
            "examPaperId":  self.examPaperId,
            "questionId": questionId,
            "version": version
        }

        try:
            ret = self.fucker.zhidaoAiExamQuery(
                url, data, ok_code=0, key=EXAM_KEY, method="GET")
            return ret["data"]
        except Exception as e:
            if triedTimes < 3:
                logger.error(
                    f"getQuestionContent failed, retrying... {triedTimes} times")
                return self.getQuestionContent(questionId, version, triedTimes + 1)
            else:
                logger.error(
                    f"getQuestionContent failed, retried 3 times, giving up...")
                return None

    def saveAnswer(self, questionId: int, answers: list):
        if len(answers) == 0:
            return False
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/answer/saveAnswer"

        data = {
            "recruitId": self.courseId,
            "examTestId": self.examTestId,
            "examPaperId": self.examPaperId,
            "questionId": questionId,
            "dataVos": None,
            "answer": '#@#'.join(str(answer) for answer in answers),
        }

        try:
            ret = self.fucker.zhidaoAiExamQuery(
                url, data, ok_code=0, key=EXAM_KEY, method="POST")
        except Exception as e:
            logger.error(f"saveAnswer failed: {e}")
            return False

        return True

    def submitExam(self, triedTimes: int = 0):
        url = "https://studentexamtest.zhihuishu.com/gateway/t/v1/exam/user/submit"

        data = {
            "examTestId": self.examTestId,
            "courseId": self.courseId,
            "courseType": 8,
            "examPaperId": self.examPaperId,
            "aiKnlowledgeId": self.knowledgeId,
        }

        try:
            ret = self.fucker.zhidaoAiExamQuery(
                url, data, ok_code=0, key=EXAM_KEY, method="POST")
        except Exception as e:
            logger.error(f"submitExam failed: {e}")
            if triedTimes < 3:
                logger.error(
                    f"submitExam failed, retrying... {triedTimes} times")
                return self.submitExam(triedTimes + 1)
            else:
                logger.error(
                    f"submitExam failed, retried 3 times, giving up...")
                raise e

        finally:
            self.examStopped = True

        return True

    def getQuestionAnswer(self, questionDict: dict) -> tuple[list, str] | None:
        questionId = questionDict["id"]
        questionType = questionDict["questionType"]
        version = questionDict.get("version", 1)

        # 尝试从缓存中获取答案
        answer = self.getAnswer(questionId, version)
        if answer is not None:
            answer = answer.get("answer", "").split('#@#')

            # 随机睡眠3-5秒，模拟网络延迟
            time.sleep(randint(3, 5))
            return answer, "cached"

        # 答案不存在，获取题目内容，使用AI生成答案
        questionContent = questionDict["content"]

        # 选项，只保留有id和content的选项
        choices = [
            {"id": option["id"], "content": option["content"]}
            for option in questionDict["optionVos"]
            if "id" in option and "content" in option
        ]

        # 选项数量少于2个，答案就是选项内容
        if len(choices) < 2:
            answer = choices[0]["id"]
            return [answer]

        try:
            # 选项数量大于2个，使用AI生成答案
            op = self.op

            if questionType == 1:
                # 单选题
                prompt = op.singleChoiceTemplate(questionContent, choices, referenceMaterials=self.referenceMaterials)
            elif questionType == 2:
                # 多选题
                prompt = op.multipleChoiceTemplate(questionContent, choices, referenceMaterials=self.referenceMaterials)
            elif questionType == 14:
                # 判断题
                prompt = op.judgementTemplate(questionContent, choices, referenceMaterials=self.referenceMaterials)
            else:
                raise ValueError(f"Unsupported question type: {questionType}")
            answer = op.generateAnswer(prompt)
            return answer, "AI generated"
        except Exception as e:
            # 随机生成答案
            if questionType == 1:
                answer = self.select_random_answers(choices, 1)
            elif questionType == 2:
                answer = self.select_random_answers(choices, 2)

            time.sleep(randint(3, 5))

        return answer, "random"

    def select_random_answers(self, choices, n):
        # 确保 n 不大于 choices 的长度
        n = min(n, len(choices))

        # 随机选择 n 个选项
        selected_choices = sample(choices, n)

        # 返回选中选项的 id
        return [choice['id'] for choice in selected_choices]

    def startFuck(self, referenceMaterials: list = [dict(name="参考资料", url="https://www.zhihuishu.com/course/10]", content=str)]) -> tuple[bool, int, int]:
        self.referenceMaterials = referenceMaterials

        # 加载答案缓存
        self.readAnswerCache(self.examTestId)

        # 打开考试
        self.openExam()

        # 获取考试试卷内容
        sheetContent = self.getSheetContent()
        if sheetContent is None:
            raise ValueError("sheet has no content")

        # 遍历试卷内容，获取每道题目的答案
        index = 0
        total_questions = len(sheetContent)
        if self.progress_view :
            progressBar(index, total_questions, "fucking exam",
                        suffix=f"{index}/{total_questions}")
        for questionDict in sheetContent:
            # 获取题目内容
            questionContentDict = self.getQuestionContent(
                questionDict["questionId"], questionDict.get("version", 1))

            if questionContentDict is None:
                logger.error(
                    f"getQuestionContent failed: {questionDict['questionId']}")
                continue

            # 获取题目答案
            questionContentDict.version = questionDict.get("version", 1)
            answer, note = self.getQuestionAnswer(questionContentDict)

            if answer is None:
                logger.error(
                    f"getQuestionAnswer failed: {questionDict['questionId']}")
                continue

            # 保存答案
            self.saveAnswer(questionDict["questionId"], answer)

            if self.progress_view :
                action = f"fucking exam"
                index += 1
                progressBar(index, total_questions, action,
                            suffix=f"({index}/{total_questions}) {note}")

        # 提交考试
        self.submitExam()

        # 重新取得试卷内容，检查是否答对了，并更新答案缓存
        total_questions = len(sheetContent)
        correct_questions = 0
        for questionDict in sheetContent:
            # 取得版本
            version = questionDict.get("version", 1)
            # 获取题目内容
            questionContentDict = self.getQuestionContent(
                questionDict["questionId"], version)

            if questionContentDict is None:
                logger.error(
                    f"getQuestionContent failed: {questionDict['questionId']}")
                continue

            # 取得做题结果
            result = questionContentDict.get("userAnswerVo", None)
            if result is None:
                logger.error(
                    f"userAnswerVo not found: {questionDict['questionId']}")
                continue
            # 计算得分
            if result[0]["isCorrect"] == 1:
                correct_questions += 1
            else:
                logger.error(
                    f"Question {questionDict['questionId']} is not correct")

            # 获取题目答案
            answer = [{"id": option["id"], "content": option["content"]}
                      for option in questionContentDict["optionVos"]
                      if option.get("isCorrect", 0) == 1]
            answer_str = '#@#'.join([str(option["id"]) for option in answer])
            answer_content_str = '\n'.join(
                [option["content"] for option in answer])

            # 保存答案
            answer_dict = {
                "question": questionContentDict.get("content", ""),
                "answer": answer_str,
                "answer_content": answer_content_str,
                "questionDict": questionContentDict,
            }
            self.setAnswer(questionDict["questionId"], version, answer_dict)

        # 如果所有题目都做对了，则退出考试
        if correct_questions == total_questions:
            logger.info(
                f"Exam {self.examTestId} is finished, score: {correct_questions}/{total_questions}")
            return True, correct_questions, total_questions
        else:
            logger.info(
                f"Exam {self.examTestId} is not finished, score: {correct_questions}/{total_questions}")
            return False, correct_questions, total_questions

