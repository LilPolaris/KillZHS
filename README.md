# KillZHS

智慧树 / 知到的本地网页工作台。Windows 用户解压后双击程序，即可在浏览器中登录、
选择课程、配置 AI、设置每日计划和查看运行记录。

基于 [VermiIIi0n/fuckZHS](https://github.com/VermiIIi0n/fuckZHS) 二次开发。
原作者版权与 MIT 许可证完整保留，改动说明见 [NOTICE.md](NOTICE.md)。

**当前为 `0.1.0-beta.2` 测试版。** 已完成离线测试、浏览器操作及 Windows 打包检查；
已有本机真实扫码与共享课启动记录，完整课程完成情况、AI 课程及模型生成仍需进一步验证。
本地“已运行”不等于平台认定课程已完成，实际结果以平台为准。

## 下载使用

从 [Releases](https://github.com/LilPolaris/KillZHS/releases) 下载 **Windows x64 ZIP**，
完整解压，双击 `KillZHS.exe`。不需要安装 Python 或 Node.js。
请保留同目录的 `_internal` 文件夹。程序会打开本地网页，并把后台控制窗口最小化到任务栏。

1. 使用知到 App 扫码登录，等待课程同步。
2. 勾选课程，按需设置倍速、单门课程时限或整批任务总时长。
3. 点击「开始学习」，在任务动态中查看当前状态，随时停止。

关闭网页后程序仍在后台运行。通过控制窗口，或网页「偏好设置 → 退出并停止任务」退出。
完整说明见 [Windows 使用说明](docs/WINDOWS.md)。

![学习概览，截图为离线演示数据](docs/screenshots/overview.png)

## 功能

- 扫码登录、复用本机登录状态；同步共享课、校内课及 AI 课程。
- 搜索、筛选和多选课程；支持粘贴课程地址或手动填写课程 / 班级 ID。
- 网页配置平台 AI、自定义模型服务、流式输出和 PPT 文件处理。
- 密钥仅保存在本机，不回传到页面；可替换或清除，保存配置不触发 API 请求。
- 任务运行中也可保存 AI 配置，下一次任务生效，当前任务继续使用启动时的设置。
- 自定义服务连接检查只请求 `/models`，不发送课程或答题内容。
- AI 生成请求上限包含失败重试，达到上限停止任务；该上限不代表金额预算。
- 每日计划：选择时间、星期、课程和总时长，默认 30 分钟。
- 按整批任务的实际经过时间控制总时长，包含课程切换与网络等待。
- 同一天最多自动触发一次，重复启动受到实例锁保护。
- 最近 100 次任务历史、最近 600 条会话日志、日志导出与环境检查。
- 本机访问限制、独立用户数据目录和后台控制窗口。

## AI 课程

AI 课程需要课程 ID 和班级 ID，自动同步失败时可手动添加。
默认**跳过测验**；需要处理测验时，在「AI 配置」中启用 AI 并关闭跳过测验。
自定义服务使用兼容 Chat Completions 的接口，填写服务商提供的准确模型 ID。

PPT 处理单独配置文件服务。开启后相关课件会上传到该服务，并可能产生费用；
默认开启转换后删除本次上传的服务端文件。

<details>
<summary>查看 AI 配置界面</summary>

![AI 配置，截图为离线演示](docs/screenshots/ai.png)

</details>

## 每日计划

例如：每天 20:00 运行所选课程，**所有课程合计最多运行 30 分钟**。
按电脑本地时间执行，程序保持后台运行、电脑开机联网且登录有效时才可执行。

- 到时结束当前批次；进度取决于平台最近一次接收的记录，下次从平台已有进度继续。
- 错过时间不补跑；该时刻有其他任务运行时跳过，并记入历史。
- 当天的尝试会先保存，重新启动程序不会重复触发同一天的计划。
- 登录过期或需要验证时，查看日志并重新登录。
- 第一版不自动开机、唤醒电脑或注册开机启动项。

![定时计划，截图为离线演示](docs/screenshots/plan.png)

## 本机数据

Windows 发布版使用 `%LOCALAPPDATA%\KillZHS` 保存配置、Cookie、计划、缓存及任务历史，
升级时替换程序目录即可保留数据。源码版默认使用 `.local/webui`。
这些文件含有私人凭据，不要分享整个用户数据目录。
原项目的 `config.json`、`cookies.json`、`execution.json` 不会被覆盖。

## 从源码运行

需要 Python 3.12 或以上，安装原有运行依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe killzhs.py --desktop
```

Windows 也可双击 `启动KillZHS.bat`。普通终端模式使用 `python killzhs.py`。
保留原命令行入口 `main.py`，上游文档见 [原项目说明](docs/UPSTREAM_README.md)。

```powershell
# 离线演示，不连接平台或模型
python killzhs.py --demo

# 更换端口；不自动打开浏览器
python killzhs.py --port 8766 --no-browser

# 无控制窗口的后台服务模式
python killzhs.py --headless --no-browser
```

## 开发与构建

```powershell
python -m unittest discover -s tests -v
python -m pip install -r requirements-build.txt
python scripts/build_windows.py
```

Windows 包需要在 Windows 上构建。产物位于 `output/release`，附带 SHA-256 校验值。
构建脚本使用明确的资源清单，不包含开发者的配置、Cookie、模型密钥或运行记录。

## 来源与许可

上游基线：`VermiIIi0n/fuckZHS@04cbbb8`。原有课程逻辑归上游作者及贡献者所有。
KillZHS 新增网页控制、AI 配置、计划与历史、Windows 分发流程，并修复了无终端环境、
API 地址拼接、PPT 服务地址与文件隔离等接入问题。

本项目采用 [MIT](LICENSE)。与原作者及智慧树平台无隶属关系，请按课程及平台规则使用。
