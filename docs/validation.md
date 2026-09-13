# v0.1.0 验证记录

验证日期：2026-09-11。以下结果来自本次实际执行；项目没有连接公司服务器或用户目标板。

2026-09-13 补充了[本机 Claude Code 实测](claude-code-validation.md)：记录真实 Agent 的 CLI/MCP 选择、Git Bash 路径修正、部署与 SSH/Telnet 前台操作，以及跨 Claude 和本地管理进程重启的长任务查询。该次未重跑本页全部底层测试。

同日新增 [Claude Skill 分发实测](claude-skill-validation.md)：从 wheel 安装工具和个人 Skill，在仓库外用 Claude 完成任务；新增安装生命周期与路径保护测试后，Windows 全量 **120 passed / 26 skipped**，WSL 全量 **146 passed**。

## 环境与结果

| 环境 / 检查 | 实际结果 |
| --- | --- |
| Windows 原生，Python 3.12.11，开发环境全量测试 | **91 passed，19 skipped**，14.66 秒 |
| Windows 原生，Python 3.11.6，独立 wheel 安装环境全量测试 | **91 passed，19 skipped**，20.34 秒 |
| WSL Ubuntu，Python 3.12.3，全量测试 | **110 passed**，18.11 秒 |
| WSL 中仅使用 BusyBox applets 的作业测试 | **27 passed**，2.65 秒 |
| Ruff 静态检查 | All checks passed |
| pip-audit（包含本地开发环境的第三方依赖） | 未发现已知漏洞；本项目未发布到 PyPI，项目自身被明确跳过 |
| wheel / sdist 构建 | `uv build` 成功；独立环境导入路径确认为 `site-packages/remote_mng` |
| Windows 安装包 → WSL Linux 的 SSH 完整操作 | 主机验证、密钥认证、SFTP 上传/摘要校验、命令执行、helper 安装和持久作业查询通过 |
| Windows 安装包 MCP → 同一作业 | stdio 初始化、查询退出码和日志通过；MCP 退出后独立 daemon 仍在线 |

Windows 跳过项需要真实 POSIX shell、进程组和相关远端工具；对应项已在 WSL 执行。WSL 测试验证的是 Linux 内核与用户态行为，不能替代独立 Linux 发行版、物理设备和厂商固件验收。

BusyBox 来自 Ubuntu 官方 `busybox-static 1.36.1-6ubuntu3.1`，下载并解压到用户缓存，未安装为系统组件。测试时将 PATH 限定为这套 applets；Python 测试运行器通过绝对路径调用，helper 自身不依赖 Python。

## 关键端到端证据

额外使用短期、只监听回环地址且要求临时密钥认证的 AsyncSSH 服务，把 **Windows 本地安装包**与 **WSL Linux 进程环境**连起来：

1. 独立 Python 3.11 环境从 wheel 安装，启动本地 daemon。
2. 加载临时目标配置，校验独立生成的 SSH 主机密钥。
3. 上传带空格文件名的 19 字节产物；读取进度，完成 SHA256 校验。
4. 运行 `uname -s`，获得退出码 0；安装随包分发的 helper。
5. 以 `windows-wheel-proof` 提交打印 begin、等待 12 秒、打印 finished、退出 7 的脚本；收到 `state=running`。
6. 停止本地 daemon。原提交连接和本地观察进程均不再持续跟踪该任务。
7. 启动新的 daemon，按相同 job_id 查询：`state=failed`，`exit_code=7`；日志准确为 `begin\nfinished\n`，字节游标为 15。
8. 使用安装包的真实 MCP stdio 接口查询同一退出码与日志，再确认 MCP 退出未结束独立 daemon。

这是持久作业可重新查询的实际证据；它没有使用预设成功状态替代远端进程。作业刻意以非零退出，验证工具能区分任务已结束和任务成功。

## 自动测试覆盖

| 测试文件 | 重点 |
| --- | --- |
| `tests/test_transports.py` | 真实 SSH/Telnet 协议、主机身份校验、stdin/stdout/stderr、持续终端、超时/掉线、16 MiB 输出限制、文件传输、路径竞态、取消卡住的传输，以及两种协议上的 helper 查询 |
| `tests/test_jobs.py` | 真实 Linux 作业分离、同 ID 幂等/冲突、退出证据、取消身份验证、重启/证据缺失的未知状态、二进制日志及 BusyBox 兼容 |
| `tests/test_core.py` | 应用接口贯穿真实协议、共享输入控制权、旧提示符、敏感回显、日志上限、传输结果、本地 Manager 重启后持久作业查询 |
| `tests/test_core_review.py` | 启动锁释放、未知退出、断开会话释放容量、Unicode 跨页、控制权与关闭竞态、GBK 和跨快照凭据遮罩 |
| `tests/test_cli_mcp.py` | 参数与退出码、原始终端代码路径、官方 SDK stdio、工具返回结构、CLI/MCP 共享状态、Windows daemon 生命周期 |

协议测试服务使用 AsyncSSH 与 telnetlib3，在真实套接字上通信；它们不是系统 OpenSSH sshd，也不是厂商 Telnet 服务。人工键盘/控制台部分使用模拟输入验证代码路径；未宣称已完成用户实际控制台的人工接管验收。

## 复现

在项目根目录执行：

```sh
uv sync --locked
uv run ruff check .
uv run pytest -q
uv build
uv run pip-audit
```

BusyBox 专项可在 Linux 准备独立 applets 目录后执行，下面两条路径需要指向你的测试环境：

```sh
PATH=/absolute/path/to/busybox-applets PYTHONPATH=src \
  /absolute/path/to/test-venv/bin/python -m pytest tests/test_jobs.py -q
```

仓库提供 Windows/Ubuntu × Python 3.11/3.12/3.13 的 GitHub Actions 配置，但**尚未上传或执行云端 CI**，因此不将该矩阵列为已通过。Actions 版本按 [checkout 官方用法](https://github.com/actions/checkout) 和 [setup-python 官方用法](https://github.com/actions/setup-python) 核查。

## 现场试用边界

- 需要真实设备确认 SSH 认证策略、Telnet 提示符与编码、BusyBox 裁剪能力，以及服务器是否主动清理脱离登录会话的进程。
- 普通终端不会在网络断开后自动恢复；只有 helper 管理的非交互式作业具备本版持久查询能力。
- 原生终端显示、Ctrl 键组合、窗口尺寸、复杂全屏应用尚未进行用户现场验收。
- 未验证远端 Windows、ARM/MIPS 物理设备、企业 MFA、任意多跳和独立 Linux 发行版矩阵。
- 同 ID 幂等以保留的远端请求目录为依据；删除记录、远端断电或存储损坏不属于无条件精确一次执行保证。
- 远端日志不自动轮转，本地默认每条流保留 64 MiB。独立命令超过 16 MiB 输出停止观察并报告未知，应改用持久作业分页读取。

本报告描述功能和故障语义验证，不包含尚未测量的吞吐、长时间稳定性、资源占用或生产设备兼容结论。
