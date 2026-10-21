# 安装工具和 Claude Code Skill

remote-mng 随工具一起分发个人 Skill。开发者向 Claude Code 终端描述需求，Agent 读取 Skill 调用 CLI；本地网页显示设备、任务、日志和更新状态。无需另外复制 Skill 或配置 MCP。

## 安装与更新

推荐从 [README 的 Windows 或 Linux/WSL 入口](../README.md#一次安装之后用自然语言)下载完整离线安装器：Windows 双击 EXE，Linux/WSL 运行 RUN，程序与 Skill 一起安装并启动管理器。离线更新带入并运行新版安装包；日常也可告诉 Claude“更新 remote-mng”，由它按安装方式引导。

**[0.6.0 预发布版](https://github.com/LX-Cel/remote-mng/releases/tag/v0.6.0)** 的完整安装器不依赖联网下载，也不要求预装 Python、uv 或 GitHub CLI。可选在线脚本与旧版维护见 [高级安装指南](distribution.md)。

Windows 原生、WSL 和 Linux 各自安装，工具路径、认证环境和状态目录不混用。Windows 原生 Claude Code 需要 Git Bash。部分 Windows 宿主限制独立后台进程，安装和更新会据实际结果给出阻碍；不能通过已预启动管理器推断后续更新也一定成功，不能借计划任务或其他宿主绕过限制。

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

日常可以直接告诉 Claude“更新 remote-mng”。0.6 Skill 先读取更新方式：离线安装提示带入并运行新版完整安装包，在线安装使用已有更新入口；不在内网主动查 GitHub。工具保留任务与原 job ID，并处理受支持安装的管理器停启和 Skill 更新。旧 uv/Python 环境暂不支持离线迁移，完整状态、恢复和限制见 [更新指南](updates.md)。

0.3 没有 `update`，运行中的旧管理器也缺少新版维护预留协议。新入口会报告阻碍并保留它，让 Agent 核对活动，在你确认普通前台可以断开后处理；不能为完成升级强停业务会话。受支持的 0.4 uv 安装到 0.5 已通过两平台真实前台升级，见 [0.5 验收记录](v05-validation.md)；旧版本的历史证据保留在 [0.4 验收记录](v04-validation.md)。

## 源码手工维护

<details>
<summary>展开：只适合明确选择源码或 Python 环境维护的开发者</summary>

准备 Python 3.11+、Git 和 uv。在公开仓库中安装固定 tag：

```sh
git clone --branch v0.4.0 https://github.com/LX-Cel/remote-mng.git
cd remote-mng
uv tool install .
rmg setup --start-daemon
```

这个旧版本示例不表示 0.5 已发布。普通 uv 环境会绑定持久工具路径；一次性临时环境不适合作为安装位置。若工具目录不在 PATH，按 uv 提示运行 `uv tool update-shell` 并重开终端。当前不通过 PyPI 包名安装。

手工升级源码安装时，先让 Agent 核对活动前台、操作和原 job ID；在源码仓库中选择已核验的 tag 或提交，处理本地修改，不能用 reset 覆盖。确认普通前台可以断开后，在实际安装环境中停止管理器，再重装：

```sh
rmg server stop
uv tool install --reinstall .
rmg setup --start-daemon
rmg doctor --json
```

这个多命令流程没有统一更新器的完整事务保护，只供明确选择源码手工维护的场景。`git pull` 只更新源码，不代表程序、管理器和 Skill 已更新。日常优先使用安装脚本或 Agent 统一更新。

重启后按原 ID 查询持久 job；普通终端不会自动接回，不能因为旧前台消失就重发测试。新开 Claude 会话加载新版 Skill；凭据环境变量也必须在新管理进程启动时存在。

</details>

## 完整性保护与卸载

```sh
rmg skill status --json
rmg skill install
rmg skill uninstall
```

相同内容重复安装不会创建副本；工具或 Skill 更新后，安装器更新内容与解释器绑定。已有同名但不是本工具管理的目录、被用户修改的文件或额外文件，会阻止覆盖和卸载。

需要保留自定义内容时，先备份并把整个同名 Skill 目录移到别处，再安装新版本；也可以恢复原文件、移走额外文件后重试。仅删掉一个已修改的必需文件会变成“文件缺失”，仍不能通过完整性检查。不要用强制覆盖跳过保护。

后续个人约定放到 `~/.claude/remote-mng/USER.md`；自定义 Claude 配置目录时跟随该目录，实际路径由 `skill status --json` 的 `user_extension.path` 返回。安装后的 Skill 会指导 Agent 在文件存在时读取。安装、更新和卸载都不会创建或修改该文件，其变化也不会导致托管 Skill 完整性冲突。原来直接修改托管目录的内容仍受保护，不自动搬移。具体示例和更新状态见 [更新指南](updates.md#保留个人-skill-约定)。

卸载只处理安装清单验证通过的 Skill，不删除设备配置、本地任务、远端 helper 或作业。工具环境移动或删除后，用可用的持久工具环境重新安装；完整性正常时可更新绑定。

自定义或隔离测试目录可显式指定：

```sh
rmg skill install --claude-dir /path/to/claude-config
rmg skill status --claude-dir /path/to/claude-config --json
```

参数是 Claude 配置目录，安装器在其中创建 `skills/remote-mng`。后续 Claude 会话也必须使用该配置目录。

## 源 Skill 与安装后的 Skill

原始资源位于 `src/remote_mng/assets/claude_skill/remote-mng/`。手工复制原始目录时，源包装脚本调用 PATH 中的 `rmg`，因此必须确保 Claude 能找到工具。安装器生成的版本绑定本机 Python 路径或独立包稳定入口，不适合直接复制到另一台机器；换机器后在当地重新安装。

当前 [0.6.0 为预发布版](https://github.com/LX-Cel/remote-mng/releases/tag/v0.6.0)，提供完整单文件离线安装器；实际 CI、制品来源和平台边界见 [0.6 验收记录](v06-validation.md)。[0.5 历史记录](v05-validation.md)、[0.4 历史记录](v04-validation.md)、[0.3 验证](v03-validation.md) 等分别记录当时范围，不能替代本版验收。
