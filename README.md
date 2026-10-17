# KillZHS 0.2.3

智慧树 / 知到本地网页学习工作台。基于旧版 `fuckZHS` 重构，整理为可独立运行和维护的项目；原作者版权与历史保留。

支持扫码登录、课程同步、共享课 / 校内课 / AI 课、学习时限、每日计划、通知、历史日志、共享课测验与考试，以及官网验证入口。AI 请求、课件上传和自动交卷均由用户在工作台主动配置或启动。

本次重构以离线假平台、请求基准和浏览器验证为依据；真实账号登录、平台累计学习时长和真实交卷仍需本人确认。演示结果不代表平台接受了进度或答案。

0.2.3 在“测验与考试”页新增成绩显示和“刷新成绩”：展示平台公布的分数 / 总分，区分待批阅与未公布成绩。刷新只读取列表，不进入试卷、不请求模型或交卷。详见 [0.2.3 说明](docs/releases/v0.2.3.md)。之前的 [保存诊断与恢复](docs/releases/v0.2.2.md) 和 [题目编号修复](docs/自动答题编号修复-2026-10-17.md) 保留。

## Windows 使用

从 [0.2.3 发布页](https://github.com/LilPolaris/KillZHS/releases/tag/v0.2.3) 下载 Windows x64 安装包，安装时可选择是否创建桌面快捷方式，默认不勾选。安装到当前用户目录，不需要管理员权限，可从 Windows 应用列表卸载；卸载保留账号设置和运行记录。

也提供免安装 ZIP：完整解压，双击 `KillZHS.exe`，保留旁边的 `_internal` 文件夹。程序打开本地工作台，后台控制窗口可停止任务或退出。说明见 [Windows 使用说明](docs/WINDOWS.md)。

新版数据保存在 `%LOCALAPPDATA%\KillZHS\v2`，不读取旧版数据；首次使用需要重新扫码和配置。源码版默认数据目录为 `.local/data`。演示模式只使用临时数据，不连接平台、模型或通知服务。

## 源码运行

支持 Windows x64、Python 3.12 / 3.14。先在项目目录准备虚拟环境及锁定依赖，再在 PowerShell 启动：

```powershell
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe -B -m killzhs
```

仅查看演示：

```powershell
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe -B -m killzhs --demo
```

依赖安装使用 `requirements-lock.txt`；开发和构建分别使用 `requirements-dev.txt`、`requirements-build-lock.txt`。可用 `pip install -e . --no-deps` 注册 `killzhs` 启动命令。前端使用原生 ES 模块，无构建步骤。

## 开发验证

```powershell
.venv\Scripts\python.exe -B -m pytest
.venv\Scripts\python.exe -B -m ruff check src tests scripts
.venv\Scripts\python.exe -B scripts/lock_dependencies.py --check
.venv\Scripts\python.exe -B scripts/browser_smoke.py
.venv\Scripts\python.exe -B scripts/compare_ui.py
.venv\Scripts\python.exe -B scripts/verify_release.py
```

测试使用离线假平台；AI 提示处理依赖预先准备的公开 `cl100k_base` 分词缓存 `.local/tokenizer/`。CI 展示了测试前下载并校验固定 SHA256 的步骤。浏览器检查需要已安装的 Playwright Chromium。界面对比还需要只读的旧目录 `../fuckZHS`，可用 `--legacy-root` 指定其他位置。请求基准已经提交，普通测试不运行旧项目，也不改写基准。

Windows 构建前检查和打包：

```powershell
.venv\Scripts\python.exe -B scripts/build_windows.py --check
.venv\Scripts\python.exe -B scripts/build_windows.py
.venv\Scripts\python.exe -B scripts/build_installer.py --check
.venv\Scripts\python.exe -B scripts/build_installer.py --update-manifest
```

构建只使用已有工具和分词缓存，不安装依赖、不下载数据。每次构建生成独立目录，最新路径与 SHA256 见 `output/release/latest-build.json`。可用 `scripts/verify_release.py --exe <KillZHS.exe路径> --output output/acceptance/frozen.json` 验证打包程序的演示启停和真实子进程环境检查。

安装器构建需要另行准备官方 Inno Setup 编译器及中文译文，固定版本、校验值和静默安装选项见 [安装器说明](docs/INSTALLER.md)。

## 结构与记录

- `src/killzhs/app`：动作、任务状态、计划、历史、HTTP 与桌面入口。
- `src/killzhs/worker`：统一的 spawn 子进程、管道事件及协作停止。
- `src/killzhs/platform`、`ai`：显式接收会话、停止控制、进度与请求预算的客户端。
- `web/js`：按视图拆分的 ES 模块，共用状态、传输和表单助手。
- [重构设计](docs/superpowers/specs/2026-10-17-killzhs-refactor-design.md)、[重构进度与验收](docs/重构进度-2026-10-17.md)。

上游作者版权和 MIT 许可见 [LICENSE](LICENSE)，改动来源见 [NOTICE.md](NOTICE.md)。
