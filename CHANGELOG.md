# Changelog

## 0.2.0 — 个人试用，尚未发布 Release

本版围绕 Agent 的执行成功率和开发者查看、管理状态的体验改进，暂不引入团队账号或多人调度。

- 增加持久任务档案：稳定任务和步骤 ID、提交前记录意图、参数冲突检测、未知请求不盲目重发、任务与 operation/session/job 关联。
- 新增 `session step` 和 `session step-get`，把输入及观察绑定到请求 ID；原请求可继续观察而不重复发送。改进前台退出状态识别，减少重复退出。
- 本地日志改为保留近期输出，轮转后继续接收；提供单调逻辑游标、`base_offset` 和 `gap`，明确报告已丢失的历史。
- 增加 `schema`、本地 `doctor`、只读 `target inspect` 和结构化参数错误，帮助 Agent 发现能力与恢复路径。
- 增加本地个人控制台，查看最近设备检查、任务时间线、活动与会话、增量日志；支持按需查询远端状态及请求取消受管任务。
- 增加个人项目 `rmg-project.json` 和 `project inspect`，只校验文件、计算摘要并生成待执行参数，不自动构建或执行配方。
- 更新随包分发的 Claude Code Skill、私有仓库认证安装和旧管理进程升级说明。
- 明确任务“步骤完成”不推断业务测试通过；普通终端断线不自动恢复，普通 exec 输出在执行返回后可查，远端 helper 日志暂未增加配额或轮转。

安装路线见 [个人试用指南](docs/personal-trial.md)，实际验证条件与结果见 [0.2 验证记录](docs/v02-validation.md)。0.1 的验收报告保留为历史记录。

## 0.1.0 接入补充 — 2026-09-13

- 当时在 GitHub 以 MIT 许可证公开分发，提供固定版本安装链接、wheel/sdist 和 SHA-256 校验文件；仓库随后改为私有，现需认证访问。
- 随 wheel 分发自包含的 Claude Code Skill，新增本地 `rmg skill install/status/uninstall`，支持跨业务项目使用、完整性检查与保护用户修改。
- Skill 安装入口绑定当前工具环境并独立处理 Git Bash 路径，无需业务仓库的 `CLAUDE.md`、MCP 或项目环境配置。
- 增加 Claude Code 项目指令与 Agent CLI 操作指南，默认由 Agent 执行命令，MCP 保持可选。
- 增加项目级 Git Bash 参数路径配置，避免远端 POSIX 路径被改写为 Windows 本地路径。
- 记录真实 Claude Code 的传包、部署、SSH/Telnet 前台和跨管理进程重启的长任务验收；详见[实测报告](docs/claude-code-validation.md)。

## 0.1.0 — 2026-09-11

首次交付通用 CLI + MCP 远程开发工具。

- SSH/Telnet 持续终端、独立命令、输入控制权移交、可配置进入/退出和增量输出。
- SFTP 上传下载与目录传输、遗留 SCP 文件传输、进度和明确的校验/覆盖语义。
- 当前用户的本地 daemon、共享目标与 SQLite 操作历史、认证 RPC 和已知凭据脱敏。
- 同版分发的 Linux Shell helper：持久请求、独立受管作业、状态/日志/取消、同 ID 幂等及断线后重新查询。
- Windows、WSL/Linux 的安装方式，Claude Code MCP 示例，自动测试及验证说明。

实际兼容范围和首版限制见 [验证报告](docs/validation.md)。
