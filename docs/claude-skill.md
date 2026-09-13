# 安装工具和 Claude Code Skill

remote-mng 0.2 随 Python 安装包分发个人 Skill。你在业务项目中用自然语言给 Claude 下任务，由它读取 Skill 并调用 CLI；你通过本地网页查看和管理状态。不需要记忆日常 CLI 命令，也不依赖 MCP。

## 从私有仓库安装

准备 Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和已配置 GitHub 认证的 Git。仓库目前为 **PRIVATE**，先确认当前账号有权限，再执行：

```sh
git clone https://github.com/LX-Cel/remote-mng.git
cd remote-mng
uv tool install .
rmg skill install
```

已经绑定 GitHub 账号的 SSH 密钥也可用于克隆：`git clone git@github.com:LX-Cel/remote-mng.git`。认证交给本地 Git 凭据管理器或 SSH，不要将访问令牌放进 URL、脚本或模型对话。仓库访问权限和本机远端设备认证是两件不同的事。

旧文档中的匿名 Release 下载地址不再适用于私有仓库。已获得 wheel 文件时仍可执行 `uv tool install ./remote_mng-0.2.0-py3-none-any.whl`，再执行 `rmg skill install`；此例不表示已经发布 0.2 Release。当前不通过 PyPI 包名安装。

也可在自己的 Python 3.11+ 持久虚拟环境中执行 `python -m pip install .`，并在该环境中安装 Skill。不要从一次性的临时环境安装：Skill 入口绑定当前工具的 Python 环境，该环境之后仍需存在。

如果安装器提示工具目录不在 PATH，运行 `uv tool update-shell` 后重开终端。Windows 原生、WSL 和 Linux 各自安装，工具路径、设备认证环境和状态目录不混用。

## 开始个人试用

默认安装位置是 `~/.claude/skills/remote-mng/`；若设置了 `CLAUDE_CONFIG_DIR`，则使用该目录中的 `skills/remote-mng/`。安装不会添加 MCP、修改业务项目的 CLAUDE.md、认证配置或宿主权限规则。个人 Skill 的加载机制见 [Claude Code 官方文档](https://code.claude.com/docs/en/skills)。

安装后在业务仓库开启**新的 Claude Code 会话**。可以直接说：

> 使用 remote-mng，检查 lab 设备并打开本地状态控制台。把本次构建产物上传，按照我的脚本部署和验证。建立任务档案，保存步骤和作业 ID；结果不明时先查原任务，不要重复部署。

或明确使用 Skill：

```text
/remote-mng 先检查本地工具和测试设备，再打开控制台
```

Claude 用 Skill 的入口调用 `rmg schema`、`doctor`、任务、会话和作业命令；用户从网页看设备状态、步骤证据及日志。`/remote-mng` 是可选显式入口。宿主的 Skill 启用条件与权限设置仍然生效，安装文件不会绕过这些条件。

首次连接需提供目标别名、SSH/Telnet 地址与端口、认证引用、传包入口和业务验证条件。私钥只提供本地路径，口令由本机环境变量引用；不要把私钥内容或密码发给模型。仅有 Telnet 服务不等于能用 SCP 传包。

构建命令、部署脚本和业务成功标准由业务项目提供。Skill 是通用远程操作说明，不内置某个测试产品的命令。完整试用顺序及个人配方见 [个人试用指南](personal-trial.md)。

## 为什么能跨项目工作

安装的 Skill 包含：

- `SKILL.md`：触发条件、入口、任务和结果判断约定。
- `references/`：连接配置、传包、可靠交互、长作业和个人任务等按需说明。
- `scripts/rmg.sh`：进入工具安装环境，调用同一个 CLI。

Claude 从 Skill 实际目录调用入口，不需要切回 remote-mng 源码仓库，也不依赖宿主启动时的 PATH 恰好包含 `rmg`。入口保持业务当前目录、标准输入输出和退出码，文本使用 UTF-8；隔离当前目录和 `PYTHONPATH` 对工具模块的遮蔽。

Windows Git Bash 调用原生程序时，包装入口会禁止 MSYS 参数自动转换，防止远端 `/tmp/...` 被改为 Windows 路径。本地文件使用 `C:/...` 或相对路径，不依赖 `/c/...` 自动转换。这个处理不需要修改业务仓库的 `.claude/settings.json`。

Skill 入口只封装本地启动，不另建一套远程连接。CLI、本地管理进程和网页应使用相同的 `RMG_HOME` 或绝对 `--home`，才能看到同一组任务。

## 升级旧版本

先让 Agent 查询当前活动状态，保存作业 ID 和必要的日志游标。0.1 没有 task 命令，可以先用旧能力：

```sh
rmg server status --json
rmg session list --json
rmg operation list --json
rmg job list lab --json
```

`lab` 替换为实际配置的目标；逐个核对正在使用的目标。0.2 用户还可以查 `task list`。旧管理进程不会热加载新版代码；升级之前确定普通交互终端何时可以断开，受管 job 则保存原 ID 供重连查询。

在本地源码仓库拉取有权限访问的更新，并重新安装：

```sh
git pull --ff-only
uv tool install --reinstall .
rmg skill install
rmg --version
rmg skill status --json
```

如果有源码修改导致 Git 无法快进，先处理本地改动，不用 reset 覆盖。上面只更新工具和 Skill，仍运行的旧管理进程保留旧代码。**确认普通终端可断开后**，再让 Agent 执行：

```sh
rmg server stop
rmg server start
rmg doctor --json
```

重启后按原 ID 查询 `job status`，在新版中使用 `task get --refresh` 查询已有任务。普通终端不会因重启自动接回；不能因为原前台消失就重发测试命令。此前运行的 0.1 操作历史可以查询，但不会自动生成 0.2 整体任务档案。

更新后新开 Claude 会话，让它加载新版 Skill。若只是凭据环境变化，值必须在新管理进程启动时存在；另一个终端里新增变量不会更新旧管理进程。

## 完整性保护与卸载

```sh
rmg skill status --json
rmg skill install
rmg skill uninstall
```

相同内容重复安装不会创建副本；工具或 Skill 更新后，安装器更新内容与解释器绑定。已有同名但不是本工具管理的目录、被用户修改的文件或额外文件，会阻止覆盖和卸载。

需要保留自定义内容时，先备份并把整个同名 Skill 目录移到别处，再安装新版本；也可以恢复原文件、移走额外文件后重试。仅删掉一个已修改的必需文件会变成“文件缺失”，仍不能通过完整性检查。不要用强制覆盖跳过保护。

卸载只处理安装清单验证通过的 Skill，不删除设备配置、本地任务、远端 helper 或作业。工具环境移动或删除后，用可用的持久工具环境重新安装；完整性正常时可更新绑定。

自定义或隔离测试目录可显式指定：

```sh
rmg skill install --claude-dir /path/to/claude-config
rmg skill status --claude-dir /path/to/claude-config --json
```

参数是 Claude 配置目录，安装器在其中创建 `skills/remote-mng`。后续 Claude 会话也必须使用该配置目录。

## 源 Skill 与安装后的 Skill

原始资源位于 `src/remote_mng/assets/claude_skill/remote-mng/`。手工复制原始目录时，源包装脚本调用 PATH 中的 `rmg`，因此必须确保 Claude 能找到工具。安装器生成的版本绑定本机 Python 路径，不适合直接复制到另一台机器；换机器后在当地重新安装。

0.2 实测见 [本版验证记录](v02-validation.md)。[0.1 Skill 实测](claude-skill-validation.md) 是历史验收，不代表新版功能已经按同一条件全部验证。
