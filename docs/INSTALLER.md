# Windows 安装器

安装器默认将 KillZHS 安装到当前用户的 `%LOCALAPPDATA%\Programs\KillZHS`，支持 Windows 10 / 11 x64，不请求管理员权限。向导为简体中文，可以选择安装位置，始终创建开始菜单中的启动和卸载入口；“创建桌面快捷方式”默认不勾选，完成页的“启动 KillZHS”也默认不勾选。静默安装不会启动程序。

安装版使用 `%LOCALAPPDATA%\KillZHS\v2` 保存用户数据。卸载只移除安装器登记的程序文件和快捷方式，不清理该数据目录，也不迁移旧版配置或登录状态。原来的便携包不会自动删除；确认新版可用后再由使用者处理。

## 从现有构建生成安装器

先生成并验证 onedir 包。安装器读取 `output/release/latest-build.json` 中的版本、bundle、archive 和 ZIP 的 sha256，不重新构建应用，也不从源码版本号猜测包版本。`--bundle` 必须与清单中的目录一致；只提供 `--bundle` 时，默认读取它上级目录的 `build.json`。清单中的相对路径相对于清单目录解析。

本地须事先提供官方 Inno Setup 编译器，默认位置为 `.local/tools/inno/ISCC.exe`。脚本不会下载或安装任何工具。CI 固定使用官方 **6.7.3** 发布包，SHA-256：

```text
9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732
```

CI 同时验证发行包的 Authenticode 发布者为 Pyrsys B.V.，然后以 `/PORTABLE=1` 模式放入构建目录。[官方发布页](https://jrsoftware.org/isdl.php)、[6.7.3 安装脚本](https://github.com/jrsoftware/issrc/blob/is-6_7_3/setup.iss)。生成的 KillZHS 安装器本身没有代码签名。

简体中文译文不在该编译器的二进制发行包里。需要预先把[官方源码 6.7.3 标签收录的完整中文译文](https://github.com/jrsoftware/issrc/blob/is-6_7_3/Files/Languages/Unofficial/ChineseSimplified.isl)放到编译器旁的 `Languages/ChineseSimplified.isl`，或用 `--language-file` 指定位置。它是官方仓库收录的社区译文，SHA-256 固定为：

```text
7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278
```

在项目目录运行：

```powershell
python -B scripts/build_installer.py --check
python -B scripts/build_installer.py
# 使用指定编译器、清单和 bundle：
python -B scripts/build_installer.py --iscc .local/tools/inno/ISCC.exe --manifest output/release/latest-build.json --bundle '实际 onedir 目录' --check
```

`--check` 只读校验，不调用编译器，不创建输出，也不更新清单。它会验证 AMD64 PE 主程序、非空 `_internal`、ZIP 校验值和中文译文，拒绝符号链接、junction、用户运行数据和不安全的路径字符。

每次实际构建写入全新的 `output/installers/<版本>-<随机值>/`，含 `KillZHS-<版本>-windows-x64-setup.exe`、该安装器自己的 `SHA256SUMS.txt` 和扩展的 `build.json`。可用 `--output-root` 指定独立输出根目录；不能放进源 bundle 内。构建前后检查源 bundle 的文件哈希，发现并发修改就失败。

默认保留源清单和 ZIP 校验文件原样。只有显式传入 `--update-manifest`，才向所选源清单加入 `installer`、`installer_sha256`、`installer_checksums`、`installer_build` 字段；原来的 `archive`、`sha256` 和其他字段保留。输出目录自身的 `build.json` 总是包含这些安装器字段。脚本不发布版本，不修改 Git，也不操作已安装程序。

构建清单和验收 JSON 可能包含本机绝对路径，仅供本地使用。CI 的发布构件只包含 ZIP、安装器和 `SHA256SUMS.txt`，不上传这些 JSON 收据。

## 安装、升级和验收

先从工作台退出正在运行的 KillZHS，确认任务停止，再升级或卸载。升级保持同一个 AppId，通过 Windows Restart Manager 检测占用并协调关闭应用，不使用虚构的 AppMutex，也不强杀进程。若程序不能正常关闭，应取消安装后自行退出再重试；安装结束不自动重启应用。[Inno 关闭应用说明](https://jrsoftware.org/ishelp/topic_setup_closeapplications.htm)。

交互安装时可以自行勾选桌面快捷方式。静默安装支持标准任务参数，例如：

```powershell
# 不创建桌面快捷方式，默认也不会启动应用：
Start-Process -FilePath '实际安装器.exe' -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /MERGETASKS=!desktopicon /NOFORCECLOSEAPPLICATIONS' -WindowStyle Hidden -Wait
# 创建桌面快捷方式：
Start-Process -FilePath '实际安装器.exe' -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /TASKS=desktopicon /NOFORCECLOSEAPPLICATIONS' -WindowStyle Hidden -Wait
```

`/TASKS=desktopicon` 选择桌面入口；`/MERGETASKS=!desktopicon` 取消选择。取消选择不会删除已经存在的同名桌面快捷方式，所以无图标验收应在没有旧入口的测试环境进行；勾选后会将同名入口指向安装目录。[任务参数说明](https://jrsoftware.org/ishelp/topic_setupcmdline.htm)。

实际验收应覆盖：无图标安装、带图标安装、快捷方式目标、开始菜单入口、默认不启动、重复安装/升级、卸载登记和用户数据保留。现有桌面便携入口的最终替换、旧程序删除和发布由父任务统一处理。单元测试只模拟编译边界，不代表完成真实安装验收。
