# remote-mng

主要供 Agent 使用的通用远程开发工具：**用户用自然语言描述任务，Claude Code 等 Agent 通过 `rmg` CLI 执行远程操作，MCP 作为可选接入方式。**

用户无需记住 SSH、SCP 或终端交互命令。Agent 可以在本地修改代码、调用现有构建流程后，通过 CLI 上传产物、运行部署脚本、连续输入测试命令，并查询状态与日志。自然语言理解与任务规划由宿主 Agent 完成；`remote-mng` 提供执行能力，不内置模型或自然语言命令入口。

CLI 是 Agent 的首选入口：使用现有的命令执行工具，按需读取 `--help`，通过 `--json` 处理结果。SSH/Telnet 会话由本地管理进程持有，多个 CLI 调用共享会话及操作记录。需要 MCP 的宿主可选用相同核心能力。

首版 `0.1.0` 已包含轻量远端作业辅助程序。它让受管作业的状态和日志留在远端，原 SSH/Telnet 连接断开、本地 CLI 退出或本地管理进程重启后，可以使用同一个作业 ID 重新查询。

想先直观了解原理和优缺点，可以用浏览器打开 [交互原理演示](docs/explainer.html)：包含架构图、断线模拟、优缺点对比及开发流程示例。页面离线运行，不会连接设备或执行命令。

## 快速开始：Claude Code

准备好 **Python 3.11+** 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，安装工具与随包分发的 **remote-mng Skill**：

```sh
uv tool install "https://github.com/LX-Cel/remote-mng/releases/download/v0.1.0/remote_mng-0.1.0-py3-none-any.whl"
rmg skill install
```

也可以先从 [GitHub Releases](https://github.com/LX-Cel/remote-mng/releases/latest) 下载 wheel，再执行 `uv tool install ./remote_mng-0.1.0-py3-none-any.whl`。如果 uv 提示工具目录不在 PATH，运行 `uv tool update-shell` 后重开终端。

它安装到 Claude 的个人 Skill 目录，供不同业务项目使用。随后在自己的代码仓库开启 Claude，可以直接描述远程任务，也可以用 `/remote-mng` 明确调用。无需给每个项目添加 `CLAUDE.md`、配置 MCP，或让用户记住日常操作命令。完整的两步安装、更新与卸载见 [Claude Skill 接入指南](docs/claude-skill.md)。

Skill 的 Bash 入口绑定本机工具安装环境，保持业务项目当前目录，并处理 Windows Git Bash 的远端路径转换。它不会写入宿主权限白名单或认证配置。源码中的 [Skill 入口](src/remote_mng/assets/claude_skill/remote-mng/SKILL.md) 与按需参考文件会随 wheel 一起分发。

本仓库的 [CLAUDE.md](CLAUDE.md) 和 [Agent 操作指南](docs/agent-guide.md) 继续供维护本工具的 Agent 使用；它们不再是其他开发者接入的前提。

已完成[独立业务项目的 Skill 实测](docs/claude-skill-validation.md)：从 wheel 安装工具和 Skill，Claude 自动加载技能，完成部署、SSH/Telnet 前台测试及跨会话作业查询。

已用本机 Claude Code 2.1.218 完成[实际工具调用验收](docs/claude-code-validation.md)：在 MCP 同时可用的情况下，修正后的部署、SSH/Telnet 前台及长任务恢复流程使用 41 条 `rmg` 子命令、0 次 MCP 调用；报告包含 Windows 路径问题的修正与测试边界。

安装并准备好目标配置后，用自然语言给出目标、任务和验收条件。例如：

> 使用 remote-mng，把本次构建产物部署到已配置的 lab 目标，执行项目中约定的部署和验证脚本。长任务使用可在断线后查询的作业，保存作业 ID；完成后告诉我实际运行版本、验证结果和相关日志。如果结果未知，先查询，不要重复部署。

Agent 应使用你指定或已配置的目标、路径和业务规则；缺失的连接信息、部署脚本与成功条件需要补齐。工具当前提供命令帮助和 JSON 结果，尚不提供 CLI 机器可读 schema 或自动发现配置的能力。

下面的命令是供 Agent 和维护者查阅的操作参考。日常任务可以继续用自然语言交给 Agent，无需逐条手工执行。

## 安装

本地支持 Windows 原生、WSL、Linux，要求 **Python 3.11 或更新版本**。上面的 Release 安装不需要克隆源码。也可以通过 Git 安装固定版本：

```sh
uv tool install "git+https://github.com/LX-Cel/remote-mng.git@v0.1.0"
rmg skill install
```

如果需要修改工具源码，先克隆仓库，再在项目根目录安装：

```sh
git clone https://github.com/LX-Cel/remote-mng.git
cd remote-mng
uv tool install .
rmg skill install
rmg --version
rmg --help
```

也可在你自己的 Python 虚拟环境中安装：

```sh
python -m pip install .
rmg skill install
rmg --version
```

开发与测试使用仓库锁文件：

```sh
uv sync --locked
uv run rmg --help
uv run pytest
```

`python -m remote_mng` 与 `rmg` 是相同入口。Agent 若找不到 `rmg`，可以调用安装环境中可执行文件的绝对路径，或在安装后重新启动宿主以更新其 `PATH`。选择 MCP 时也可在其配置中填写绝对路径。

### Windows Git Bash 路径约定

使用已安装 Skill 的入口时，这些处理已由包装脚本完成，不需要给业务仓库添加项目配置。以下说明适用于直接调用原生 CLI 或维护本源码仓库的场景。

Git Bash/MSYS 调用原生程序时可能将远端 `/home/...` 改写为 Windows 本地路径。项目 [.claude/settings.json](.claude/settings.json) 仅设置环境变量 `MSYS2_ARG_CONV_EXCL=*`，让 Claude Code 在本项目调用原生 `uv`、Python、`rmg` 时保留这些参数；不包含权限规则、凭据或用户全局修改。该变量关闭的是 Git Bash 对原生程序参数的路径自动转换，不只针对远端参数。用法依据 [MSYS2 官方路径文档](https://www.msys2.org/docs/filesystem-paths/)。

因此，本地文件使用 `E:/workspace/...` 形式或相对路径，不依赖 `/e/...` 的自动盘符转换；远端路径保持 `/tmp/...`、`/home/...` 等真实 POSIX 路径。配置作用于加载本项目设置的 Claude Code 及其子进程，其他启动环境不会自动获得这项项目配置。WSL/Linux 原生 shell 和 PowerShell 无需 MSYS 路径转换，该变量不改变它们的路径处理。

## 启动共享管理进程

```sh
rmg server start
rmg server status
```

CLI 一般会按需启动管理进程。Windows 的进程宿主可能限制独立子进程启动或清理子进程树；遇到 `daemon_start_failed`，或需要会话跨宿主退出保留时，应从独立终端预先运行 `rmg server start`，再让 Agent 连接。**选择 Windows MCP 时，独立启动是必需步骤**，其入口不自动启动管理进程。Linux/WSL MCP 可以按需启动。

CLI 与 MCP 使用相同的本地状态目录才能共享连接。默认目录为当前用户的 `~/.remote-mng`；也可以统一设置 `RMG_HOME`，或为每次调用加上 `--home`：

```sh
rmg --home ./local-rmg-state server start
rmg --home ./local-rmg-state target list
```

Agent 使用自定义 `--home` 时请保持同一个绝对路径，避免工作目录变化后读到另一套状态；MCP 也遵循这一约定。Windows 原生与 WSL 是两个独立运行环境，分别安装、分别启动，使用各自的路径、凭据环境及状态目录。

`rmg server stop` 会停止本地管理进程并断开它持有的普通终端连接；受管远端作业继续运行。它不是暂停所有远端业务程序的命令。

## 配置目标

将以下内容保存为本地 `lab.json`。`192.0.2.10` 为文档示例地址，请替换地址、用户和路径：

```json
{
  "protocol": "ssh",
  "host": "192.0.2.10",
  "port": 22,
  "username": "developer",
  "client_keys": ["~/.ssh/id_ed25519"],
  "known_hosts": "~/.ssh/known_hosts",
  "shell": "posix",
  "connect_timeout": 15,
  "transfer_timeout": 3600,
  "encoding": "utf-8",
  "helper_dir": "~/.local/share/remote-mng"
}
```

先通过你信任的渠道核对服务器主机密钥，并让对应记录存在于配置指定的 `known_hosts` 中。所有 SSH 连接都校验主机密钥，不会自动接受未知或变化的密钥。导入后检查连接：

```sh
rmg target add lab --file ./lab.json
rmg target list
rmg target check lab
```

`target add` 会替换同名目标配置。文件中不放密码或私钥内容；密码认证用 `password_env` 指向本地环境变量名，加密私钥口令用 `passphrase_env`。Windows JSON 路径可以写作 `C:/Users/developer/.ssh/id_ed25519`。

Telnet 的登录步骤、密码回显脱敏、独立 SSH 文件入口、主机信任与兼容范围详见 [连接与传输指南](docs/transports.md)。例如，Telnet 终端目标可以在 `transfer` 配置中指定用于 SCP/SFTP 的 SSH 主机、端口与认证。仅有 Telnet 服务的设备没有天然的 SCP 文件通道。

Telnet 默认 `shell: "unknown"`，适用于专用设备菜单或应用前台；明确登录到 POSIX shell 后才设置为 `"posix"`，以使用命令包装和作业辅助程序。Telnet 是明文协议，环境变量引用不会改变线上传输的明文属性。

## 独立命令与日志

短命令可以直接等待结果：

```sh
rmg exec lab "uname -a" --wait
rmg exec lab "pwd" --cwd /tmp --env LANG=C --wait
```

复杂命令建议写入 UTF-8 本地脚本，避免经过 PowerShell、Bash 和远端 shell 的多次引号解释：

```sh
rmg exec lab --script ./remote-check.sh --cwd /tmp --json
```

默认快速返回 `id`，后续使用该 ID 查询。下面的 `OPERATION_ID` 需要替换为实际返回值：

```sh
rmg operation list
rmg operation get OPERATION_ID
rmg operation logs OPERATION_ID --stream stdout --offset 0 --limit 65536
rmg operation logs OPERATION_ID --stream stderr --offset 0 --limit 65536
```

继续读取时使用上次返回的 `next_offset`。SSH exec 具有分别的 stdout、stderr 和退出状态；POSIX Telnet 命令的输出流合并。独立命令不继承其他交互会话的目录和环境。

**需要断线后仍可查的长命令请用 `job start`。** 普通 `exec` 的记录保存在本地，但它不具备远端持久作业的生命周期保证。观察超时、网络错误或缺失退出状态会返回未知结果，工具不会自动重发命令。

## 上传与下载

先用你现有的构建系统出包，再传输明确的产物：

```sh
rmg file upload lab ./package.tar.gz /tmp/package.tar.gz --wait
rmg file download lab /tmp/results.log ./results.log --wait
```

默认使用 SFTP。目标只有遗留 SCP 时显式选择协议：

```sh
rmg file upload lab ./package.tar.gz /tmp/package.tar.gz --protocol scp --overwrite --wait
```

远端路径必须为绝对路径，表示**最终文件名或最终目录名**。默认不覆盖已有文件；有意替换时加 `--overwrite`。不加 `--wait` 会返回操作 ID，用 `operation get` 查询进度和结果。

SFTP 支持 `--recursive` 和 SHA-256 读回校验。遗留 SCP 首版只传普通文件，目录需要先打包；其结果明确报告未进行端到端摘要校验。递归传输不是整棵目录的原子事务，也不删除目标中多出来的文件。精简设备对原子发布命令及 SFTP 扩展的要求见 [协议与覆盖语义](docs/transports.md#文件传输与覆盖语义)。

上传完成只说明传输完成。部署、版本确认与功能验证仍由你的脚本或 Agent 使用具体证据完成。

## Agent 连续操作交互终端

Agent 可以通过非交互 CLI 调用打开、读取和写入同一个远程终端：

```sh
rmg session open lab
```

也可以指定通用交互配置。保存下面的演示程序为本地 `demo-console.sh`，然后上传：

```sh
#!/bin/sh
while :; do
  printf 'demo> '
  IFS= read -r line || break
  case "$line" in
    ping) printf 'PONG\n' ;;
    long) sleep 2; printf 'DONE\n' ;;
    quit) break ;;
    *) printf 'Unknown command: %s\n' "$line" ;;
  esac
done
```

```sh
rmg file upload lab ./demo-console.sh /tmp/demo-console.sh --wait
rmg session open lab --profile-file ./examples/interactive-profile.json
```

配置示例 [interactive-profile.json](examples/interactive-profile.json) 定义进入命令、提示符、退出命令及观察超时。应用提示符和外层 shell 提示符均按正则匹配，实际使用应改为对应程序的规则。

打开结果包含 `id` 与随机的 `control_token`，由 Agent 保存并用于后续调用。将下列占位符替换为返回值；`WRITE_CURSOR` 必须使用本次 `session write` 返回的 `cursor`：

```sh
rmg --json session write SESSION_ID ping --newline --token CONTROL_TOKEN
rmg --json session wait SESSION_ID PONG --offset WRITE_CURSOR --timeout 10
rmg --json session read SESSION_ID --offset 0
```

`--regex` 可以启用正则等待。先写入、再用写入前的游标等待，才能避免把旧提示符当作本次命令已完成。`input_sent` 只表示输入已发送，`matched: true` 只表示观察到相应文本，都不代表业务测试通过。

需要中断前台时，可单独执行 `rmg session write SESSION_ID --key ctrl-c --token CONTROL_TOKEN`；这会改变应用状态，不是常规读取步骤。

完成交互后，Agent 按任务需要退出应用、交还写入权或关闭终端：

```sh
rmg --json session leave SESSION_ID --token CONTROL_TOKEN
rmg --json session close SESSION_ID --token CONTROL_TOKEN
```

`leave` 使用交互配置中的 `exit` 命令退出应用，保留终端连接；未配置退出命令时明确失败。`release` 仅交还写入权；`close` 关闭终端连接，不能据此断言远端后台程序已停止。

Agent 应使用 `write/read/wait` 完成连续交互，不需要打开本地交互控制台。输入也可通过 `session write --data-file FILE` 或 `--data-file -` 传入；使用标准输入时调用方需要写入完整数据并关闭 stdin，避免等待人工键入。

### 可选诊断：人工临时接管

需要开发者亲自检查终端时，可以显式接管同一个会话：

```sh
rmg session attach SESSION_ID --force
```

`--force` 撤销原控制令牌，Agent 随后不能继续写入。接管期间按 **Ctrl+]** 脱离并交还输入权，远端终端保持打开；Ctrl-C 作为控制字符发向远端。也可用 `--line-mode` 进行行输入。人工退出后，Agent 重新 `claim` 取得新令牌，核对状态与新输出后继续：

```sh
rmg --json session claim SESSION_ID
rmg --json session get SESSION_ID
rmg --json session read SESSION_ID --offset LAST_CURSOR
```

原生交互控制台的按键、终端模拟和显示效果尚未在用户真实终端中现场验收，实际验证范围见 [验证记录](docs/validation.md)。这项诊断能力不属于 Agent 日常操作的必需步骤。

人工敏感输入可通过 `session write --data-file - --sensitive` 从标准输入传入，也可用 `RMG_CONTROL_TOKEN` 代替命令行中的令牌。不要将密码写进 shell 历史、MCP 参数或 Agent 对话。脱敏只覆盖已知敏感值及显式标记的输入，不能识别任意程序输出中的所有秘密。

## 断线后可查询的长任务

首版辅助程序是可检查的 POSIX shell 脚本，不需要远端 Python，不增加监听端口。目标需要 Linux `/proc`、可写目录、`setsid` 和安装探测所检查的常见命令。安装时会核对必需能力，裁剪固件不满足要求时明确失败；不要求每个普通 SSH/Telnet 终端都安装辅助程序。

```sh
rmg helper install lab
rmg job start lab "sleep 10; printf 'finished\n'" --job-id demo-sleep-001
rmg job status lab demo-sleep-001
rmg job logs lab demo-sleep-001 --stream stdout --offset 0
rmg job list lab
```

辅助程序默认写入远端 `~/.local/share/remote-mng`，可通过目标的 `helper_dir` 配置修改。作业脚本、状态和 stdout/stderr 都保存在该目录下。长部署脚本可按原样提交：

```sh
rmg job start lab --script ./deploy-and-check.sh --cwd /tmp --job-id deploy-001
```

受管作业在连接断开、本地客户端退出后继续。需要验证本地重启后的恢复查询时，在确认普通终端可以断开后执行：

```sh
rmg server stop
rmg server start
rmg job status lab deploy-001
rmg job logs lab deploy-001 --stream stderr --offset 0
```

重要约定：

- 提交前自己指定稳定的 `--job-id`，可以在提交响应丢失时直接查这个 ID；自动生成的 ID 也会尽可能放入错误详情和本地操作记录。
- 同一个 ID、相同的命令/目录/环境请求会返回已有作业，不重复执行；同一 ID 对应不同请求会报冲突。完成的 ID 也不会被当作新任务再次执行。
- 断线后先查 `job status` 或 `job list`。不能因为没收到成功响应就换一个 ID 重发部署。
- 远端重启、退出记录丢失或进程身份不一致时会报告 `unknown`。断线可恢复查询不等于跨远端重启自动续跑，也不保证恢复任意交互前台内部的业务任务。
- 在后台自行脱离受管进程组的程序不属于可完整跟踪的范围；`job start` 跟踪的是提交脚本及其正常生命周期。

有意取消时使用：

```sh
rmg job cancel lab deploy-001
rmg job status lab deploy-001
```

取消请求与实际终止分别报告。工具核对 Linux 启动身份和进程启动时间，不仅凭旧 PID 发信号；仍需查询状态确认结果。

`exec`、文件传输与 `job start` 都支持 `--wait`；`--wait-timeout 30` 仅限制本地等待，超时不会发送取消。CLI 退出码：`0` 表示调用完成或所等待操作成功，`1` 表示错误/失败，`2` 表示结果未知，`124` 表示本地等待超时，`130` 表示本地等待被中断。默认异步返回成功不代表远端作业已经成功。

## 可选：通过 MCP 接入

Claude Code 等能够执行本地命令的 Agent 可以直接使用上述 CLI，无需先配置 MCP。需要 MCP 接口时，本工具通过官方 Python MCP SDK 提供 stdio 服务，stdout 仅承载协议。Windows 请先从独立终端启动管理进程，然后使用 Claude Code CLI 注册：

```sh
rmg server start
claude mcp add --transport stdio --scope user remote-mng -- rmg mcp
claude mcp get remote-mng
```

或将 [claude-mcp.json](examples/claude-mcp.json) 的内容合并到项目 `.mcp.json`：

```json
{
  "mcpServers": {
    "remote-mng": {
      "type": "stdio",
      "command": "rmg",
      "args": ["mcp"]
    }
  }
}
```

不要覆盖已有 MCP 配置。项目级信任与工具权限由宿主管理；Claude Code 的参数、作用域与 `.mcp.json` 格式见 [官方 MCP 文档](https://code.claude.com/docs/en/mcp)。其他支持 stdio MCP 的 Agent 可使用同样的 `rmg mcp` 命令。

自定义状态目录时，给 CLI 与 MCP 相同的绝对 `--home`，或在 MCP 服务的 `env` 中设置相同的 `RMG_HOME`。环境继承有两层：

1. `RMG_HOME` 和可执行文件路径需要在 CLI/MCP 进程中正确设置。
2. `password_env`、`passphrase_env`、Telnet `send_env` 指向的变量值，必须存在于**本地管理进程启动时**的环境中。在另一个终端或 MCP JSON 中新增变量，不会更新已经运行的管理进程。

需要修改凭据环境时，先为当前终端设置变量，再停止并重新启动管理进程。该重启会断开普通交互终端，受管远端作业的持久记录仍可查询。不要把密码值直接写入 MCP JSON 或分享给模型。

主要工具按能力分别命名，包括 `target_list`、`exec_start`、`operation_get`、`session_open/read/write/wait/claim/release/leave/close`、`file_upload/download`、`helper_install`、`job_start/status/list/logs/cancel`。返回结构化结果和增量日志游标，Agent 不需要持续占用一个长时间阻塞的工具调用来保留会话。

## 首版边界与验证

- 普通持续终端依赖本地管理进程存活。管理进程或远端连接丢失后，历史输出仍可查询，但原终端不会自动恢复。
- 持久作业需要 Linux 与辅助程序能力；不把无法观测的任务标为成功或失败。
- 遗留 SCP 不提供目录递归、摘要读回验证或断点续传。SFTP 的校验会额外读取数据；两种协议不会静默互相降级。
- 首版不包含 GUI、端口转发、串口、跨目标事务、多用户设备调度、GDB 专用协议或通用 MFA 自动登录器。复杂 SSH 跳板配置仅覆盖依赖库实际支持的配置项。
- 本地每个输出流默认最多保留 64 MiB，达到上限时报告 `log_truncated`，后续内容不再保存；可在管理进程启动前设置 `RMG_MAX_LOG_BYTES`。远端作业日志和历史记录没有自动轮转/清理策略，需要监控存储空间。首版不提供自动删除历史的命令。
- 本地管理进程按当前用户运行，RPC 仅监听 loopback 并要求认证。执行权限最终来自远端账户；目标动作限制不是 shell 沙箱或远端目录权限隔离。
- 尚无用户真实公司服务器、嵌入式目标板及应用终端记录。协议测试和通用 Linux 测试不等于所有设备已兼容。详见 [验证记录](docs/validation.md) 与 [连接与传输指南](docs/transports.md)。

完整范围见 [首版需求](docs/requirements-draft.md)，实现职责见 [架构说明](docs/architecture.md)，锁定依赖与许可证声明见 [依赖说明](docs/dependencies.md)。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方依赖保留各自的许可证，详见 [依赖说明](docs/dependencies.md)。
