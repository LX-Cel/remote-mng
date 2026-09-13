# Claude Skill 分发实测

验证日期：2026-09-13。已从真实 wheel 安装工具和个人 Skill，并在源码仓库之外的业务项目中，用本机 Claude Code 2.1.218 完成自动 Skill 调用、部署、SSH/Telnet 前台操作和持久任务查询。

## 安装和运行环境

- 使用独立的 `UV_TOOL_DIR` 与 `UV_TOOL_BIN_DIR` 执行真实 `uv tool install <wheel>`，得到新的工具环境。导入位置确认为该环境的 `site-packages/remote_mng`，不是源码路径。
- 使用新工具执行 `rmg skill install --claude-dir <隔离Claude配置目录>`。安装返回 `installed`，完整性为 `verified`，且没有启动管理进程。
- 独立业务项目位于系统临时目录，路径包含空格和单引号；有自己的 Git 根目录，没有 `CLAUDE.md`、`.claude/settings.json` 或产品源码。
- Claude 的 PATH 未追加新安装的 `rmg`。Skill 通过自身目录内的 Bash 入口，调用绑定的安装环境。
- 在业务项目内放置了会报错的同名 `remote_mng.py` 哨兵文件。实际操作仍进入安装包；另有测试验证外部 `PYTHONPATH` 不能遮蔽工具。
- 保留用户现有认证与模型环境映射，主模型仍报告 `glm-5.3[1m]`；使用隔离 Claude 配置，没有安装 MCP 或修改真实用户的全局配置。
- 非交互验收使用 `dontAsk`，仅在本次调用参数中允许 Skill(remote-mng)、Read 和 Bash 包装调用。分发的 Skill 本身不携带权限白名单或动态命令注入。

两轮 Claude 初始化均把 `remote-mng` 列入 Skill 和斜杠命令列表；自然语言任务没有提供底层命令序列，Claude 自行调用 `Skill` 工具加载 `remote-mng`，再读取按需参考文件并使用包装脚本。共记录 2 次 Skill 加载、45 次经入口脚本执行的 CLI 调用，MCP 调用为 0。

## 实际操作

| 项目 | 实测证据 |
| --- | --- |
| 首次目标配置 | Claude 从业务项目内 JSON 登记 `skill-ssh` 和 `skill-telnet`，连接检查成功 |
| 本地服务首次启动 | 安装 Skill 没有创建运行状态目录；Claude 第一次查询目标时自动拉起管理进程，未要求用户先启动服务 |
| 上传与校验 | SFTP 上传 784 字节测试包，SHA-256 与本地一致：`d9a7aa70dda4a1c7b0c112ab8e76b5c453941346398fc1919b9d72821ea15751` |
| 首次远端 helper 安装 | Claude 在两个目标安装随工具分发的轻量辅助程序 |
| 部署 | 执行项目给出的脚本，退出码 0，日志包含 `DEPLOY_OK version=1.0.0-skill` |
| 前台交互 | SSH 和 Telnet 均取得 `VERSION=1.0.0-skill`，烟测输出 `RESULT PASS; cases=3 failed=0` |
| 长任务 | Claude 提交稳定 ID `skill-durable-001`，观察到 `running` 与 `LONG_JOB_BEGIN` 后结束第一轮 |
| 跨会话查询 | 停止本地管理进程后启动新的 Claude；它自动恢复本地服务，用原 job_id 查到 `succeeded`、退出码 0 和两条完整日志，没有再次提交任务 |
| 更新后交互复验 | 新 Claude 在两个目标上完成版本检查，各调用一次 `session leave`，再关闭终端，没有重复退出输入 |

本轮长任务实际运行 25 秒，在验收脚本停止本地管理进程前已完成。因此本页证明的是**完成后跨新 Claude 会话和本地管理进程重新查询**。执行中停止管理进程、原远端进程继续运行的证据仍以此前[项目级 CLI 实测](claude-code-validation.md)为准，不将两个实验混为同一结果。

## 实测中修正的问题

首轮在最终成功前出现两个观察错误：Claude 先用小写模式匹配大写版本输出，以及把正则转义用于默认文本匹配。它还在手动退出后再次调用 `leave`，导致第二次退出命令落到外层 shell。技能说明现已明确大小写、文本与正则的区别，以及已退出时直接关闭或释放的处理；新会话复验中两个目标均只调用一次配置退出流程。

另一个真实 Git Bash 探针使用包含中文及 emoji 的路径，发现 Windows 默认 GBK 标准输出导致 `UnicodeEncodeError`。安装入口现已固定 UTF-8 标准输入输出；在继承 GBK 配置的条件下，回归测试仍可严格按 UTF-8 解析完整 JSON、保留原路径并取得退出码 0。

升级后的安装返回 `updated`，随即再次安装返回 `unchanged`。没有为升级删除原 Skill 或放宽文件完整性要求。

## 自动测试和文件保护

| 检查 | 结果 |
| --- | --- |
| Windows Python 3.12 全量 | **120 passed，26 skipped** |
| WSL Ubuntu Python 3.12 全量 | **146 passed** |
| 安装器专项，Windows | **29 passed，7 skipped** |
| 安装器专项，WSL | **36 passed** |
| Ruff | 通过 |
| Skill frontmatter、命名及完整性验证 | 通过 skill-creator 的 quick_validate |
| 文档与示例核对 | 6 个 Skill 资源、内部引用、36 条 CLI 参数示例及 3 个 JSON 模板通过 |
| 分发与真实环境 | wheel 包含 Skill，已从独立安装环境实际调用 |

安装器测试覆盖首次安装、重复安装、更新、卸载、用户修改、额外文件、未知同名目录、路径穿越、符号链接、失败回滚，以及真实包装脚本的参数、标准输入、退出码和模块路径隔离。独立审查额外验证了 Windows junction 被拒绝，指向目录的文件不变。Windows 的平台跳过项包括缺少符号链接权限和需要 Linux shell/进程环境的测试，对应覆盖已在 WSL 执行。

## 范围与证据

结构化结果见 [Skill 验收证据](claude-skill-evidence.json)。原始工具流、实验脚本与临时路径记录位于 Git 忽略的 `.test-state/claude-skill-verify-20260913/`，不要直接作为公开报告分享。

验收后已停止本地管理进程和实验服务器，确认两个实验端口不再监听，并通过安装器卸载隔离配置目录中的测试 Skill。业务文件、目标配置和作业记录保留，真实用户的 Claude 配置未修改。

SSH/Telnet 服务仅监听本机回环地址，远端运行真实 Linux 命令与测试前台。本次没有连接公司服务器或物理板卡，不能代替实际 CTP、目标固件、认证及现场编码验收。测试只证明当前本机宿主和模型组合的行为；Skill 的自动选择仍受用户任务描述与 Claude 配置影响。

面向其他开发者的操作说明见 [安装工具和 Skill](claude-skill.md)。分发机制采用标准 `SKILL.md` 及个人 Skill 目录，依据 [Claude Code 官方 Skill 文档](https://code.claude.com/docs/en/skills)，本页结论以实际工具调用为证据。
