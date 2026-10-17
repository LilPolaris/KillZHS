# KillZHS 重构设计

日期：2026-10-17
来源：`项目/学习辅助工具/fuckZHS`，分支 `codex/local-webui`，提交 `86de549`（0.1.0-beta.7）。
初始实施时旧目录只读、新项目独立维护。后续用户授权在原 GitHub 发布渠道发布重构版，因此发布提交保留远端旧历史、替换源码；本机旧源码仍保留，删除旧版仅清理旧程序包和快捷方式。

## 1. 目的

旧项目是在上游 fuckZHS 命令行脚本上逐步叠加网页工作台、AI、计划、通知、测验和验证实验形成的。
核心问题：

- `webui.py` 1080 行，控制器、HTTP、计划、历史、桌面窗口混在一起；`dispatch()` 用一长串 `if action ==` 分发。
- `fucker.py` 1581 行，上游风格，依赖 `ObjDict` 的默认值语义、`print` 树状输出和日志文本。
- worker 通过猴子补丁接入旧客户端：替换 `requests.Session.request`（计数、超时、停止检查）、`utils.getRealPath`（重定向目录）、`progressBar`（进度事件）；靠日志文本里的 `captcha`、`time limit exceeded` 判断结果。
- 源码版用管道、打包版用文件，两套 worker 通信方式；停止和验证确认靠轮询文件。
- 前端三个 JS 文件靠全局变量互相调用，HTML 由服务端拼接，CSS 2742 行，同一断点散成多个块。

目标是在**用户可见功能不变**的前提下，把代码整理成职责清晰、可测试、易维护的新项目。

## 2. 目标、非目标与反目标

**目标**

- 网页工作台的全部功能保持：扫码登录、课程同步与手动添加、共享课 / 校内课 / AI 课学习、倍速与时限、整批总时长与 30 秒收尾、AI 配置与请求上限、PPT 参考资料、测验与考试自动答题、官网内嵌与验证提示、验证恢复实验（环境变量开启）、每日计划与补跑、通知、任务历史与按次日志、异常退出恢复、实例锁、本机访问限制、演示模式、后台控制窗口、Windows 打包。
- 对平台发出的请求序列与旧版一致（字段、加密、上报节奏、随机暂停、观看点、阈值、默认倍速）。
- 界面外观、文案、交互与旧版一致。

**非目标**

- 不新增功能（如 DPAPI 加密凭据、自动识别验证码）。
- 不保留上游命令行模式：`main.py`、`config.json` 交互配置、`decrypt/`、`push.py`、上游 README 图片、账号密码登录、终端二维码。
- 不兼容旧版数据目录格式，新版从空数据开始。

**反目标**（即使能跑也算失败）

- 平台请求的字段、顺序或节奏与旧版不同，导致进度不被平台接受或更易被识别。
- 秘密值（Cookie、API Key、通知密钥）出现在页面状态、日志、历史或发布包里。
- 交卷保护被削弱：未复核就交卷、结果不明时重复交卷。

## 3. 约束

- Windows 10/11 x64；Python 3.12 与 3.14；PyInstaller onedir windowed 打包。
- 只监听 127.0.0.1，保留 Host / Origin / 令牌校验和 CSP。
- 运行依赖：`requests`、`pycryptodome`、`openai`（仅 PPT 文件接口）、`tiktoken`（提示截断）、`pydantic` v2（原本已随 openai 打包，改为显式依赖）。去掉 `Pillow`。
- 开发依赖：`pytest`、`ruff`、`playwright`；构建依赖：`pyinstaller`。
- 前端无构建步骤、无框架，原生 ES 模块。

## 4. 已确定的结构

```
KillZHS/
  pyproject.toml                项目元数据、依赖、ruff / pytest 配置
  requirements-*.txt            锁定文件（沿用旧项目的锁定与许可证收集方式）
  src/killzhs/
    __init__.py                 VERSION
    __main__.py                 入口；打包版在此调用 multiprocessing.freeze_support()
    models.py                   pydantic 模型：运行设置、AI、通知、计划、课程、历史行
    runtime.py                  TaskControl、TaskStopped、收尾
    privacy.py                  秘密收集与脱敏
    notifications.py            PushPlus / Bark 与发送队列
    app/
      cli.py                    参数、数据目录、实例锁、启动
      server.py                 HTTP 路由与安全头
      controller.py             任务状态机与事件处理
      actions.py                各动作的校验与处理（替代 dispatch 的 if 链）
      scheduler.py              每日计划
      history.py                历史、按次日志、异常恢复
      store.py                  JSON 原子读写
      desktop.py                tkinter 后台控制窗口
      demo.py                   演示模式模拟
    worker/
      protocol.py               主进程与 worker 之间的事件类型
      process.py                multiprocessing 启动、停止、强制结束
      jobs.py                   login / refresh / start / test_ai / diagnose / assessment_*
    platform/
      http.py                   会话、加密查询、停止检查与默认超时
      crypto.py                 AES、ev、观看点、校内课签名
      auth.py                   扫码登录
      shared.py                 共享课
      hike.py                   校内课
      ai_course.py              AI 课程与 AI 课考试
      assessments.py            共享课章节测验与在线考试
      verification.py           验证诊断与验证恢复实验
    ai/
      providers.py              平台 AI 与兼容 Chat Completions 的服务，请求计数在此
      ppt.py                    PPT 转文字
  web/
    index.html                  全部视图
    styles.css                  分区整理后的样式
    js/                         api、store、dom、forms、各视图、main
  tests/                        pytest；fake_platform 与协议基准
  scripts/                      打包、锁定依赖、浏览器冒烟、界面截图对比
  docs/
```

### 4.1 worker

- 用 `multiprocessing`（spawn）启动，源码版与打包版同一套代码。
- 事件经 `Pipe` 回到主进程；停止请求（含原因 stopped / limited）与验证确认经进程间对象下发。强制结束仍是 terminate，3 秒后 kill。
- 不再修改第三方库或全局函数。平台客户端显式接收：TaskControl、目录、进度回调、日志回调、AI 请求计数器。
- 结果判断用显式结果对象：需要验证、达到时限、需要查看。"需要查看"沿用旧语义：课程执行期间出现任何错误级日志即标记。
- 原始 stdout / stderr 仍不进入页面，只有结构化事件进入。

### 4.2 平台客户端

- 接口地址、加密密钥、请求字段、上报间隔、随机数调用位置和顺序与旧版一致，以便用协议基准逐条比对。
- 去掉旧客户端里与工作台无关的部分：密码登录、`fuckWhatever`、`fuckVideo`、v1 上报、缓存上报、`queryStudyReadBefore`、`revEv`、推送、图片保存、`Fucker.limit` 计时（与 TaskControl 的单门课程时限重复，且总是更晚触发）。
- `ObjDict` 改为普通字典配合显式取值。
- 树状 `print` 改为中文结构化日志；进度标题显示视频名称。

### 4.3 数据目录

- 发布版：`%LOCALAPPDATA%\KillZHS\v2`；源码版：`.local/data`；演示：系统临时目录。
- 文件：`config.json`（运行设置、AI、通知、计划）、`courses.json`、`cookies.json`、`history.json`、`task-logs/`、`schedule-state.json`、`active-task.json`、`instance.lock` / `instance.json`、`ai-answers/`（AI 课考试答案缓存，每门课一个文件）、`assessment-attempts/`（交卷日志）。

### 4.4 HTTP 接口

- `GET /api/state?since=&history_revision=`：语义同旧版（增量日志、历史按版本传输）。
- `POST /api/actions/<name>`：每个动作一个路径，请求体为该动作的参数。
- `GET /api/task-log/<id>`：需令牌。
- 静态文件：`/`、`/styles.css`、`/js/*.js`、`/favicon.svg`，白名单。

### 4.5 前端

- 单个 `index.html`；`js/` 下：`api.js`（请求与令牌）、`store.js`（轮询、增量日志、订阅）、`dom.js`（图标、转义、提示）、`forms.js`（表单已修改状态与签名比对的通用助手）、各视图模块、`main.js`（路由）。
- CSS 分区：变量、基础、布局、组件、页面，响应式集中在末尾，每个断点一个块。
- 可见外观与交互不变；只改动 4.2 提到的日志文案与进度标题。

## 5. 验证

1. **行为测试**：旧项目 80 个测试的意图全部移植为 pytest。
2. **协议基准**：`tests/fake_platform.py` 模拟平台；`scripts/record_legacy.py` 在只读的旧目录上运行旧客户端（禁止写字节码，日志与缓存重定向到临时目录），固定随机种子、模拟时钟，录下全部请求（方法、地址、查询参数、解密后的表单）到 `tests/golden/`。场景：扫码登录、三类课程列表、共享课学习（含已看视频跳过、弹题、随机暂停、验证码中止）、校内课遍历（视频 / 文件 / 测验节点 / 已完成）、AI 课（文本 / PPT / 两类视频 / 其他资源、已完成知识点、考试）。新客户端在同一假平台上必须产生相同序列；后台线程请求（视频地址获取、考试心跳）单独按集合比较。
3. **界面对比**：新旧两版演示模式，Playwright 截取每个页面在 1440 / 960 / 500 宽度的截图，遮掉日期时间后逐像素比较；保留原浏览器冒烟流程。
4. **打包检查**：Windows 打包后启动 exe，检查演示模式与子进程环境检查。
5. **真实账号**：扫码登录、同步、短时学习由用户本人操作确认。

## 6. 发现并修复的旧缺陷

实施中逐项补充。每项写明现象、原因、修复。

| 编号 | 位置（旧） | 现象 | 修复 |
|---|---|---|---|
| B1 | `killzhs_ai_exam.getQuestionAnswer` | 题目少于 2 个选项时返回列表而非二元组，调用处解包崩溃 | 返回（答案, 来源） |
| B2 | `killzhs_ai_exam.getQuestionAnswer` | 判断题模型失败时没有随机兜底，题目被跳过 | 与单选一致，随机选 1 项 |
| B3 | `killzhs_ai_exam.openExam` | 心跳线程非守护且只在交卷时停止，交卷前出错会让 worker 挂住并持续发心跳 | 守护线程，答题流程结束（含异常）时停止 |
| B4 | `killzhs_ai.Openai.__parseStream` | 流结束仍无答案块时返回 None，后续抛 TypeError | 返回已收到的文本，走"未找到答案块"的既有错误路径 |
| B5 | `fucker.getZhidaoContext` | 小节（`videoSmallLessons`）缺少 `lessonId` 字段时，`ObjDict` 默认值让上报里的 `lessonId` 变成 `{}` | 缺少时取所属课时的 `id`（与官方页面的 `this.lessonId` 一致）；平台返回该字段时行为不变 |

续作实施时发现的问题（新实现 / 未完成模块）：

| 编号 | 位置（新） | 现象 | 修复 |
|---|---|---|---|
| B6 | `app/store.atomic_json` | Windows 并发替换同一配置文件时可能出现拒绝访问 | 每次独立临时文件，串行化同进程读写；失败保留原文件 |
| B7 | `worker/jobs` 环境检查 | AI 依赖按需导入，检查可能在缺少 openai / tiktoken 时误报通过 | 环境检查显式导入全部运行依赖，不下载分词数据、不访问平台 |
| B8 | `ai/providers.ChatModel` | 流中答案提前到达或解析失败时响应未关闭；持续接收片段时不能及时响应停止 | 所有响应在 finally 中关闭，每个流片段检查 TaskControl，停止不重试 |
| B9 | `app/server` | 非 ASCII 令牌导致比较异常；Windows 未消费超限请求体可能重置连接、吞掉错误响应 | 非 ASCII 返回 403，超限时有限读取请求体后返回 400 |
| B10 | `app/controller` | 提前发出的成功消息可能掩盖随后进程失败，强制停止时迟到事件可能覆盖状态 | 退出码与结构化结果共同判断，缺少结果不能算成功；强制停止拒收迟到事件 |

## 7. 分批实施

每批完成后停下，由用户审阅再进入下一批。

0. 新建目录、git、登记知识库目录条目；假平台与录制脚本；录制协议基准。
1. 基础模块：`models`、`runtime`、`privacy`、`notifications`、`store`、`history`。
2. 平台客户端与协议比对。
3. worker。
4. 本地服务：控制器、动作、计划、HTTP、命令行、桌面窗口、演示。
5. 前端与界面对比。
6. 打包、CI、文档、最终验收。

## 8. 由实施者决定的事项（已定，可在审阅时推翻）

- HTTP 服务继续用标准库，不引入 Web 框架。
- 数据目录放在 `%LOCALAPPDATA%\KillZHS\v2`，与旧版分开。
- 版本号从 `0.2.0` 开始。
- 课程遍历日志改为中文，进度标题显示视频名。
- 视频地址获取线程改为守护线程；该请求仍会完整读取视频地址响应（与旧版一致）。
