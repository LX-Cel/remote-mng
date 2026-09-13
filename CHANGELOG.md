# Changelog

## 0.1.0 接入补充 — 2026-09-13

- 在 GitHub 以 MIT 许可证公开分发，提供固定版本安装链接、wheel/sdist 和 SHA-256 校验文件。
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
