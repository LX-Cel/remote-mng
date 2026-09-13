# remote-mng

**让 Agent 可靠地操作远程设备，让开发者看清任务进度和执行结果。**

你用自然语言向 Claude Code 描述任务，Claude 通过随包分发的 Skill 调用 `rmg` CLI。工具管理 SSH/Telnet 连接、传包、终端输入、远端长作业和持久任务档案；你通过本地网页查看设备、任务步骤、日志和待确认状态。自然语言规划由 Claude 完成，工具不内置模型。

当前版本 **0.2.0，供个人试用**。本仓库暂为 **私有仓库**，安装需要有权限的 GitHub 账号。当前重点是 Agent 执行成功率和个人状态管理；不包含团队账号、多人调度或团队流程治理。

## 安装与开始使用

准备 Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和已配置 GitHub 认证的 Git。在有权限的账号下克隆，再安装工具和 Skill：

```sh
git clone https://github.com/LX-Cel/remote-mng.git
cd remote-mng
uv tool install .
rmg skill install
```

也可以用已绑定 GitHub 账号的 SSH 密钥克隆：`git clone git@github.com:LX-Cel/remote-mng.git`。不要把访问令牌写进仓库 URL、脚本或对话。私有仓库的 Release 不能按匿名公开下载地址安装；当前源码也未发布到 PyPI。

如果 uv 提示工具目录不在 PATH，运行 `uv tool update-shell` 后重开终端。安装后的 Skill 默认在 `~/.claude/skills/remote-mng/`，可用于不同业务项目，无需给每个项目添加 `CLAUDE.md` 或配置 MCP。完整安装、升级与完整性保护见 [Claude Skill 接入指南](docs/claude-skill.md)。

在业务项目中开启新的 Claude Code 会话，直接说：

> 使用 remote-mng，先检查已配置的 lab 设备，再把本次构建产物上传并执行我的部署和验证脚本。建立任务记录，保存步骤和作业 ID；结果不明时先查询。打开本地控制台让我查看进度。

也可用 `/remote-mng` 明确调用。首次使用时，Agent 需要你提供真实的目标地址、认证引用、部署脚本和成功判据；示例地址与脚本不能替代业务配置。

**建议从 [个人试用指南](docs/personal-trial.md) 开始。** 本次验证及边界见 [0.2 验证记录](docs/v02-validation.md)；[0.1 Skill 实测](docs/claude-skill-validation.md) 和 [0.1 CLI 实测](docs/claude-code-validation.md) 保留为历史记录。

## 0.2 提供什么

| 能力 | Agent 怎么使用 | 开发者能看见什么 |
| --- | --- | --- |
| 执行前诊断 | `doctor`、`target inspect` 读取能力与修复建议 | 最近检查结果、检查时间、缺失条件 |
| 持久任务档案 | `task create` 后为动作指定稳定步骤 ID | 目标、产物摘要、步骤时间线、实际结果 |
| 可靠交互 | `session step` 把一次输入和后续观察关联到请求 ID | 发送和观察依据，结果未知的位置 |
| 断线后查询 | 远端 helper 保存受管 job 的状态和日志 | 原作业状态、最后确认状态和查询时间 |
| 连续日志观察 | 用逻辑游标增量读取；轮转后明确报告缺口 | 新日志持续可见，旧日志缺失不会被掩盖 |
| 个人项目配方 | `project inspect` 校验文件并生成待执行步骤 | 本次要用的产物、脚本及摘要 |
| 本地控制台 | Agent 执行 `rmg ui` 打开页面 | 设备、任务、会话、操作和日志，以及适用的管理按钮 |

任务显示“步骤完成”表示工具记录的步骤已有完成证据，**不自动推断业务测试通过**。上传完成、命中提示符、脚本退出码分别是不同的证据。断线导致无法确认时显示待确认，并保留最后确认的状态。

## Agent 命令参考

下面是供 Agent 和维护者查阅的命令；日常使用可以继续通过自然语言完成。所有示例中的目标、路径、ID 都应替换为实际值。

```sh
rmg schema --json
rmg doctor --json
rmg target list --json
rmg target inspect lab --directory /opt/test-app --json
rmg ui
```

`doctor` 不指定目标时只检查本地安装和已运行的管理进程，不自动建立远端连接。`target inspect` 进行只读探测，不传包、部署、安装 helper 或创建目录；SFTP 子系统可用也不等于实际目标路径已经完成一次上传。支持情况及认证配置见 [连接与传输指南](docs/transports.md)。

### 任务记录、传包与长作业

```sh
rmg task create trial-001 --title "部署并验证测试包" --target lab --artifact-file ./build/package.tar.gz --json
rmg file upload lab ./build/package.tar.gz /tmp/package.tar.gz --task trial-001 --step-id upload --wait --json
rmg helper install lab --task trial-001 --step-id helper --json
rmg job start lab --script ./scripts/deploy-and-check.sh --job-id trial-001-deploy --task trial-001 --step-id deploy --json
rmg task get trial-001 --refresh --json
rmg job logs lab trial-001-deploy --offset 0 --json
rmg task seal trial-001 --json
```

在已知需要安装或更新 helper 时才执行安装步骤。部署前先判断传输结果；部署后检查作业的退出状态和业务验证输出。`task seal` 只结束步骤提交，仍运行的作业不会因此变成成功。下一次独立试验使用新任务和作业 ID。

响应丢失后复用原任务/步骤 ID，或查询原 operation/job ID。相同任务步骤与相同参数不会重复提交；参数变化会报冲突。`--wait-timeout` 只限制本地等待，不取消远端工作。任务取消仅请求停止该任务自己提交的受管 job，不停止普通 `exec`、交互终端或其他后台业务。

### 交互前台

```sh
rmg task create trial-002 --title "检查交互前台" --target lab --json
rmg session open lab --profile-file ./console-profile.json --task trial-002 --step-id open --json
rmg session step SESSION_ID "TI VERSION" --request-id version-001 --expect "VERSION=" --token CONTROL_TOKEN --task trial-002 --step-id version --json
rmg session step-get SESSION_ID version-001 --json
rmg session read SESSION_ID --offset NEXT_OFFSET --json
rmg session leave SESSION_ID --token CONTROL_TOKEN --task trial-002 --step-id leave --json
rmg session close SESSION_ID --token CONTROL_TOKEN --task trial-002 --step-id close --json
```

`console-profile.json` 应描述真实程序的进入命令、提示符和退出方式。返回的 `control_token` 由 Agent 保存，不放入面向用户的总结。`RMG_CONTROL_TOKEN` 也可提供令牌。

`session step` 超时后可用相同请求 ID、相同输入和匹配条件继续观察；不会再次发送原输入。匹配默认为区分大小写的文本匹配，`--regex` 明确启用正则。普通 `session write` 仍存在，但不提供步骤级去重，适用于明确的底层操作和控制字符。

完成交互后优先按配置执行一次 `leave`。若已经手动退出并看见外层提示符，不再补发退出命令。`release` 只交还写入权，`close` 关闭连接，都不证明后台程序已终止。

### 个人项目配方

把 [配方模板](examples/rmg-project.json) 复制到业务仓库根目录，修改产物路径和部署脚本后：

```sh
rmg project inspect --file ./rmg-project.json --json
```

这只验证配置和本地文件、计算摘要、给出明确参数，**不构建、不上传、不执行脚本**。Agent 检查计划后逐步使用 `task invoke` 或带 `--task --step-id` 的对应 CLI 执行。实际流程见 [个人试用指南](docs/personal-trial.md)。

## 环境与安装维护

Windows 原生、WSL、Linux 均需 Python 3.11+；Windows 与 WSL 分别安装，使用各自的路径、凭据环境和本地状态目录。默认状态目录是 `~/.remote-mng`，可通过一致的 `RMG_HOME` 或绝对 `--home` 指定。CLI 通常按需启动本地管理进程。

Skill 的入口绑定当前工具环境，保持业务仓库当前目录、UTF-8 输入输出，并处理 Git Bash 的 MSYS 参数转换。本地 Windows 路径使用 `C:/...` 或相对路径，远端路径保持 `/tmp/...` 等 POSIX 形式。Skill 不写入宿主权限白名单或认证配置。

升级工具和 Skill 前，先核对活动会话与作业。旧管理进程不会热加载新代码；切换版本需要在允许普通终端断开的时机重启。具体顺序见 [升级说明](docs/claude-skill.md#升级旧版本)。不要通过反复重启解决普通连接故障。

开发和测试使用锁文件：

```sh
uv sync --locked
uv run rmg --help
uv run pytest
```

`python -m remote_mng` 与 `rmg` 是同一入口。MCP 保留为可选适配层，使用 `rmg mcp`；Agent 日常使用以 CLI 和 Skill 为主。维护者操作契约见 [Agent 指南](docs/agent-guide.md)。

## 当前边界

- 普通交互会话由本地管理进程持有。进程重启或网络连接丢失后可以读历史，但原终端和应用前台不会自动恢复。
- 普通 `exec` 在命令执行返回后保存捕获的输出，不提供执行过程的实时流式日志。它不具有远端持久作业保证；长部署和测试用 `job`。
- durable job 需要 Linux `/proc`、`setsid` 等 helper 探测能力。远端重启不会自动续跑；自行脱离受管进程组的后台程序不能完整跟踪。
- 本地每个输出流默认保留最多 64 MiB，轮转后继续接收新输出。旧游标落入缺口时返回 `gap` 和 `base_offset`，历史缺失必须纳入结论。该配额可由管理进程启动时的 `RMG_MAX_LOG_BYTES` 调整。
- **远端 helper 日志和历史没有自动配额、轮转或清理策略**；需要关注远端剩余空间。任务档案也不自动删除历史。
- SFTP 有 SHA-256 读回校验；遗留 SCP 只传普通文件，无端到端摘要验证或断点续传。Telnet 没有天然文件通道，需要单独配置 SSH 传包入口。
- 本地网页与 RPC 只监听 loopback，并要求各自的认证。网页是个人状态与管理入口，不是完整的 SSH 终端模拟器，不提供团队账号体系。
- 目标账户决定远端执行权限；动作限制不是 shell 沙箱。已知凭据脱敏不能识别任意输出中的全部秘密。
- 实际设备兼容性仍需个人试用验证；回环协议和通用 Linux 验证不代表所有嵌入式板卡及专用前台均已兼容。

首版原理可参考 [离线交互演示](docs/explainer.html)；其中首版界面和能力说明不代表 0.2 的完整功能。历史范围见 [首版需求](docs/requirements-draft.md)，依赖及许可证见 [依赖说明](docs/dependencies.md)。

## 许可证

本项目采用 [MIT License](LICENSE)。仓库当前为私有，许可证不自动授予 GitHub 仓库访问权限。第三方依赖保留各自许可证。
