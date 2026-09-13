# 安装工具和 Claude Code Skill

remote-mng 随 Python 安装包分发 `remote-mng` Skill。开发者安装工具后，再安装一次个人 Skill，就能在自己的业务代码仓库中让 Claude 使用远程能力。日常操作由 Claude 读取 Skill、规划并调用 CLI 完成，不要求用户记住命令，也不依赖 MCP。

## 两步安装

准备好 **Python 3.11+** 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，从 GitHub Release 安装到持久工具环境，再安装 Skill：

```sh
uv tool install "https://github.com/LX-Cel/remote-mng/releases/download/v0.1.0/remote_mng-0.1.0-py3-none-any.whl"
rmg skill install
```

也可以从 [Releases](https://github.com/LX-Cel/remote-mng/releases/latest) 下载 wheel 后离线提供本地文件给安装器（依赖仍需联网下载或预先准备）：

```sh
uv tool install ./remote_mng-0.1.0-py3-none-any.whl
rmg skill install
```

也可以执行 `uv tool install "git+https://github.com/LX-Cel/remote-mng.git@v0.1.0"` 安装固定版本；拿到源码时，在源码根目录执行 `uv tool install .`。项目通过 GitHub 分发，尚未发布到 PyPI，因此这里不使用在线包名安装命令。也可以在自己的 Python 3.11+ 持久虚拟环境中执行 `python -m pip install <wheel路径>`，随后在该环境执行 `rmg skill install`。

如果安装器提示工具目录不在 PATH，按安装器提示更新 PATH 并重开终端。不要从一次性的临时工具环境安装 Skill：安装器会把入口绑定到当前工具的 Python 环境，该环境需要在后续使用时保留。

默认目录为 `~/.claude/skills/remote-mng/`；设置了 `CLAUDE_CONFIG_DIR` 时使用该配置目录。安装只写入这一个 Skill 及安装管理所需的锁文件，不添加 MCP、不修改 settings、CLAUDE.md、认证或全局权限规则。Claude 的个人 Skill 可以用于不同项目，加载位置和触发方式见 [Claude Code 官方 Skill 文档](https://code.claude.com/docs/en/skills)。

安装完成后，在业务项目中开启新的 Claude Code 会话。既可以直接说：

> 使用 remote-mng，把本次构建的测试包上传到已配置的 lab 设备，执行项目的部署和自检脚本。保存长任务 ID，完成后告诉我实际测试结果。

也可以明确调用：

```text
/remote-mng 帮我配置测试设备，随后验证连接
```

`/remote-mng` 是可选的显式入口；正常情况下 Claude 可根据任务描述选择 Skill。没有启用 Skill 的宿主模式或组织策略仍可能限制发现和调用，安装文件本身不会覆盖这些限制。

## 第一次连接设备

Skill 会先查询当前配置，再根据任务补齐必要信息：目标名称、SSH/Telnet 地址和端口、认证引用、传包入口、部署路径和实际验收条件。它会使用工具已有的配置能力，不会猜测服务器或把示例当作你的业务配置。

私钥用本地路径，口令用环境变量引用；不要把私钥内容或密码发给模型。SSH 主机身份需要核验。仅有 Telnet 的设备如需传包，仍需另行提供 SSH/SFTP/SCP 入口。准备好配置后，后续可以一直使用目标名称。

构建命令、设备账号和部署脚本属于业务项目自身。Skill 提供通用远程操作知识，不内置 CTP 命令或替代项目的验收规则。

## 为什么能跨项目工作

安装的 Skill 包含三部分：

- `SKILL.md`：说明何时使用工具、如何发现命令，以及入口和结果判断约定。
- `references/`：按需读取连接配置、传包、前台交互、长任务等操作细节。
- `scripts/rmg.sh`：薄封装，直接进入安装工具时的 Python 环境，执行同一个 `remote_mng` CLI。

Claude 从 Skill 的实际目录调用这个入口；不需要切回 remote-mng 源码仓库，也不依赖 Claude 启动时的 PATH 中恰好存在 `rmg`。入口保持业务项目当前目录、标准输入输出和退出码，文本使用 UTF-8，不内置另一套远程执行逻辑。它会隔离当前目录及 `PYTHONPATH` 对工具模块的遮蔽，业务仓库中恰好存在同名 Python 文件也不会替换已安装工具。

Windows Claude 的 Git Bash 调用原生程序前，入口会禁止 MSYS 参数路径自动转换，防止远端 `/tmp/...` 被改成 Windows 本地路径。本地文件使用 `C:/...` 等原生路径或相对路径；不依赖 `/c/...` 自动转换。这不需要修改任何项目的 `.claude/settings.json`。WSL/Linux 中使用同一 Bash 入口；安装环境和运行环境要保持一致。

Skill 不预先授予工具权限，Claude 原有的权限设置仍然生效。任务能力、用户授权和宿主权限是三个不同的条件。

## 更新、检查和卸载

```sh
rmg skill status --json
rmg skill install
rmg skill uninstall
```

更新工具后再次安装 Skill，使内容及绑定的 Python 路径与当前工具一致。重复安装相同内容不重复创建目录。已有同名但不属于本工具的 Skill、被用户改动的文件或额外文件，会阻止覆盖或卸载。需要保留修改时，先备份并将整个同名 Skill 目录移到别处，再重新安装；也可以恢复原文件并移走额外文件后重试。仅删除一个被修改的必需文件会变成文件缺失，仍不会通过完整性检查。

卸载只处理安装清单验证通过的本工具 Skill，不删除目标配置、远端辅助程序、作业记录或其他 Skill。若工具环境被移动或删除，使用可用的工具环境重新执行安装；完整性正常时可更新绑定。

自定义或测试用的 Claude 配置目录可显式指定：

```sh
rmg skill install --claude-dir /path/to/claude-config
rmg skill status --claude-dir /path/to/claude-config --json
```

这个参数代表 Claude 配置目录，安装器会在其中创建 `skills/remote-mng`。后续 Claude 会话也必须使用同一个配置目录。

## 直接分发 Skill 目录

包内的 Skill 资源也可以按目录分发，手工复制到 Claude 的个人或项目 Skill 目录。源码位置是 `src/remote_mng/assets/claude_skill/remote-mng/`。未经过安装器绑定的源版入口使用 PATH 中的 `rmg`；接收方必须已经安装工具并让 Claude 能找到它。

安装器生成的目录绑定了本机 Python 路径，不应直接当作跨机器通用目录复制。向其他开发者分发 wheel 或原始 Skill 资源，再由接收方本地安装。
