# Claude Code 项目约定

本项目主要供 Agent 使用。用户用自然语言描述目标，由你负责选择、执行和核对命令；不要要求用户记忆或代为运行整套 CLI 流程。

## 默认使用 CLI

- 涉及远程命令、传包、交互调试和受管作业时，优先通过现有命令执行工具调用 `rmg` CLI。
- 已激活环境中有 `rmg` 时直接使用；否则从本仓库根目录调用 `uv run rmg`。不要为接入而全局安装工具或修改用户全局设置。
- 按需读取 `--help`，不猜参数。除帮助和人工终端诊断外，调用加 `--json` 并检查返回字段与退出码。
- MCP 是可选入口，仅在用户明确需要或任务明确要求 MCP 接口时使用；不要先配置 MCP 才执行可由 CLI 完成的任务。

## Windows Git Bash 路径

项目 `.claude/settings.json` 已设置 `MSYS2_ARG_CONV_EXCL=*`，防止 Git Bash/MSYS 把传给原生 `uv`、Python 或 `rmg` 的远端 `/home/...`、`/tmp/...` 参数改写为 Windows 本地路径。该环境设置由本项目提供，无需给每条 CLI 命令追加环境变量前缀。

- 本地路径使用 `E:/workspace/...` 形式或相对路径；不要依赖 `/e/...` 自动转换为 Windows 盘符路径。
- 远端路径保持真实 POSIX 路径 `/...`，不要改成 Windows 路径。
- 设置只作用于加载本项目配置的 Claude Code 及其子进程，并影响 Git Bash 调用原生程序时的路径参数自动转换；不修改用户全局环境。WSL/Linux 原生 shell 和 PowerShell 无需该转换，该变量不改变它们的路径处理。

仓库环境中的帮助与状态查询示例：

```sh
uv run rmg --help
uv run rmg session write --help
uv run rmg --json server status
```

`server status` 不会启动服务。后续远程操作使用用户指定或已配置的目标与授权范围，缺失的目标信息、部署规则和成功条件需要先补齐。

## 连续操作与恢复

- 保存 operation、job、session 的 ID，以及当前控制令牌和输出游标；日志按 `next_offset` 分页读取。
- 断线后需要继续查询的任务使用受管作业，首次提交前保存稳定的 `job_id`。
- 交互输入使用 `session write`，随后将本次返回的 `cursor` 交给 `session wait --offset`；Agent 默认不调用需要人工 TTY 的 `session attach`。
- 超时、断线或 `unknown` 时先查询现有 ID，不自动重放输入，不通过换作业 ID 重复提交部署。
- 业务成功根据实际验证结果判断，不能只凭输入已发送、上传完成或匹配到提示符。

详细操作约定：

@docs/agent-guide.md
