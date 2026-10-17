# KillZHS 来源说明

KillZHS 基于 [VermiIIi0n/fuckZHS](https://github.com/VermiIIi0n/fuckZHS) 开发，
上游基线提交为 `04cbbb8`。原作者的 MIT 版权与许可声明完整保留在 `LICENSE`。

本次重构以本地网页工作台版本 `0.1.0-beta.7`（提交 `86de549`）为基础，
整理为 `src/killzhs` 包，并保留网页工作台、学习任务、测验、计划与 Windows 发布流程。
KillZHS 的新增内容同样以 MIT 许可证发布，由 LilPolaris 维护，与原作者及智慧树平台无隶属关系。

依赖软件遵循各自的许可证。Windows 发布包在 `_internal/third-party-licenses/`
中附带从已安装运行依赖收集的许可证文本与 `inventory.json`；清单记录依赖名称、版本和收集到的文件。
未提供可收集许可证文件的依赖仍列在清单中。打包使用的 tokenizer 数据为 tiktoken 的
`cl100k_base` 缓存，构建时核对其公开 SHA-256，不包含账号或运行数据。
