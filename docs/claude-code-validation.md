# 本机 Claude Code 实测

本页保留最初项目级接入的验证记录。随后新增了随工具安装、跨业务项目使用的 Skill，其独立验收见 [Claude Skill 分发实测](claude-skill-validation.md)。

验证日期：2026-09-13。结论：补齐项目接入后，本机 Claude Code 能按项目约定通过 CLI 完成传包、部署自检、SSH/Telnet 连续前台操作，以及跨 Claude 进程退出和本地管理进程重启的长任务查询。修正后的三轮中，MCP 已连接并获准使用，远程操作实际均通过 CLI 完成。

## 实际环境和调用方式

- 宿主：Windows 原生 Claude Code **2.1.218**，调用本机已安装的 `claude.exe -p`，记录真实 `stream-json` 工具调用及工具结果。
- 模型：保留用户现有认证、服务地址和模型环境配置；Claude 初始化报告主模型映射为 **`glm-5.3[1m]`**。这是 Claude Code 宿主与该模型配置的验收，不代表 Anthropic Claude 模型也已测试。
- 项目：Windows Python 3.12.11 开发环境中的 remote-mng 0.1.0。默认 PATH 未安装 `rmg`，修正后的验收没有追加虚拟环境到 PATH；Claude 自行采用仓库内的 **`uv run rmg`**。
- 目标：WSL Ubuntu 内的回环 SSH/Telnet 实验服务。SSH 使用临时密钥与严格 known_hosts，Telnet 使用本机无口令测试终端；两个服务只监听回环地址。远端执行真实 Linux shell 和进程，TI 前台为测试程序。
- 隔离：独立的 `CLAUDE_CONFIG_DIR`、`RMG_HOME` 和目标目录；只加载项目设置，隔离用户全局插件和钩子。用户现有认证和模型环境值仅传入子进程，不写进项目配置或报告。
- 权限：使用 `dontAsk`，在本次启动参数中允许 `Bash(rmg *)`、`Bash(uv run rmg *)`、`Read` 和本项目 MCP 工具。没有使用跳过所有权限检查的选项，没有写入永久权限白名单。
- MCP：通过 `--strict-mcp-config` 仅加载本项目的真实 stdio 服务。修正后三轮初始化均报告 `connected` 并列出 28 个 MCP 工具；不是通过禁用 MCP 制造 CLI 优先结果。

自然语言任务只给出已授权目标、产物、路径和验收条件，没有在任务提示里逐条提供 CLI 操作。另一个禁用工具的发现检查能够说出 `CLAUDE.md`、导入的 Agent 指南及 CLI 优先约定，说明项目入口确实进入了 Claude 的上下文。

Claude Code 的程序化调用与权限方式参考其[官方文档](https://code.claude.com/docs/en/headless)，本次实际参数以本机 2.1.218 的 `--help` 为准。

## 首轮发现的问题和修改

首轮 Claude 优先调用 CLI，但 Git Bash 将上传参数中的 `/home/...` 改成了 `E:/Git/home/...`。CLI 返回的传输操作随后明确失败，错误为 `invalid_path`。模型尝试在命令前设置 MSYS 环境变量，被本次受限的 `dontAsk` 命令规则拒绝，随后使用已获准的 MCP 上传和查询接口完成了传输。因此，首轮只能算 CLI/MCP 混合验证，不能算完整 CLI 验收。

本次增加了以下项目级接入：

1. 根目录 [CLAUDE.md](../CLAUDE.md)：自动导入 [Agent 操作指南](agent-guide.md)，约定 CLI 优先、按需查询帮助、JSON 结果、稳定任务 ID 和断线后先查询。
2. [.claude/settings.json](../.claude/settings.json)：仅设置 `MSYS2_ARG_CONV_EXCL=*`，让 Windows Git Bash 向原生程序传递参数时保留远端 POSIX 路径。本地路径使用 Windows 原生形式或相对路径，不依赖 `/e/...` 自动转换。
3. README 与 Agent 指南：说明项目加载边界和 Windows 路径约定。

参数转换及该环境变量的语义见 [MSYS2 官方文档](https://www.msys2.org/docs/filesystem-paths/)。这是项目内子进程的路径配置，不包含凭据、权限放宽或用户全局修改。本次没有修改 SSH、Telnet、传输或作业的运行时代码。

## 修正后的业务证据

| 验收项 | 实际证据 |
| --- | --- |
| CLI 和 MCP 同时可用 | 三轮初始化均列出已连接的 remote-mng MCP 工具；实际远程工具调用选择 Bash 内的 `uv run rmg` |
| SSH 与 Telnet 连接 | `target check` 均成功 |
| CLI 上传 | SFTP 上传 233 字节，状态 `succeeded`，校验方式 `sha256`，远端路径保持 `/home/...` |
| 产物校验 | 本地与传输结果 SHA-256 相同：`81fb85fce549e35e0b9cab773ab09ffcaf23be960316d298fbb2db0b8aae2a87` |
| 部署自检 | 解压退出码 0；执行包内 selftest.sh 退出码 0，日志为 `SELFTEST_PASS version=claude-cli-acceptance-v1` |
| SSH 连续前台 | `TI PING` 后观察到新 `PONG`，`TI RUN SMOKE` 后观察到 `RESULT PASS; cases=3 failed=0` |
| Telnet 连续前台 | 同样观察到本次输入后的 PONG 与测试 PASS |
| 前台退出 | 两个前台均正常退出，随后关闭各自终端；未把关闭终端等同于后台进程停止 |
| 长任务提交 | 第一轮独立 Claude 进程提交 `claude-durable-001`，查到 `running` 和 `DURABLE_STARTED` 后退出 |
| 本地停止期间 | 管理进程停止后查询返回 `daemon_not_running`；WSL 中原远端 PID 仍存在 |
| 重启与新 Claude 查询 | 管理进程 PID 从 44876 变为 48036；新 Claude 查到相同 job_id、远端 PID 1280 和 started_at，仍为 `running` |
| 原长任务完成 | 新 Claude 创建测试释放文件，原任务变为 `succeeded`，退出码 0；stdout 包含起止两行标记，stderr 为空；没有再次调用 `job start` |

长任务真实运行约 144 秒。脚本在等待期间保留远端进程，收到测试释放文件后打印 `DURABLE_FINISHED_AFTER_RESTART` 并结束；这不是预置的完成状态。本地管理进程的停止和重启由验收脚本执行，任务提交、重启后的业务查询和释放则由两个独立 Claude Code 进程执行。

工具计数、关键字段和去除控制令牌的证据见 [结构化验收记录](claude-code-evidence.json)。本机原始工具流和实验脚本位于 `.test-state/claude-cli-verify-20260913/`，该目录被 Git 忽略；原始流不是可公开分享的报告。

| 修正后阶段 | Bash 工具调用 | 其中实际 rmg 子命令 | MCP 调用 | 权限拒绝 |
| --- | ---: | ---: | ---: | ---: |
| 传包、部署与两个前台 | 28 | 31 | 0 | 0 |
| 长任务提交 | 2 | 3 | 0 | 0 |
| 新 Claude 查询与完成 | 7 | 7 | 0 | 0 |
| 合计 | **37** | **41** | **0** | **0** |

一个 Bash 调用可以包含多条命令，计数包含按需执行的 `rmg --help`，因此这两个数字不能混为同一个指标。完整流程另有一次 Read 工具调用。

## 验证边界

- 已验证的是本机指定版本、模型配置与项目指令组合的实际表现；项目指令可以引导工具选择，不构成对所有模型、所有提示的强制保证。
- 只在 Claude 加载本项目指令和设置时自动生效。其他项目需要引用指南并准备相应路径设置，尚未修改用户全局 Claude 配置。
- 本次使用隔离配置，没有验收用户全局插件、钩子与其他 MCP 服务同时运行时的组合。
- 验证了连接关闭、Claude 退出和本地管理进程重启后的远端受管任务查询；没有模拟物理设备掉电、远端重启、网络黑洞或丢失提交确认。
- 目标为 WSL 中真实协议服务和模拟 TI 程序，不是公司 CTP、物理板卡或系统 OpenSSH sshd。真实固件、登录提示、认证策略、编码及测试成功条件仍需现场验收。
- 本次传包走 SFTP，没有将该轮结果表述为 Claude 驱动的 SCP 验收；首版其他协议与底层测试范围见 [v0.1.0 验证记录](validation.md)。
