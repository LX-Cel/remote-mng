# 在同一个 POSIX Shell 中执行命令

0.3 增加 `session shell-enable` 和 `session exec`。主要使用者是 Agent：它在同一个 Shell 中切换目录、设置环境、连续执行部署命令，并取得每条命令的退出码。开发者通过自然语言安排工作，通过网页和任务记录查看结果。

它与两个已有接口分工如下：

| 接口 | 状态与完成证据 | 适用场景 |
| --- | --- | --- |
| `exec` | 独立执行；退出码，不继承交互会话目录 | 单条检查命令 |
| `session exec` | 同一个已确认 Shell；本次完成帧、退出码与输出范围 | 连续部署、保留 `cd` / `export` |
| `session step` | 原始交互输入；匹配指定输出，不提供 Shell 退出码 | CTP/TI 等应用前台 |
| `job start` | 远端 helper 保存作业结果和日志 | 需要断线后查询的非交互任务 |

## Agent 的典型操作

先打开普通终端，观察实际输出。只有确定当前位于 POSIX Shell，才能启用命令封装。`--confirm-posix` 表示调用方对当前终端环境的明确判断，并不要求每次都向开发者询问；判断依据应来自任务配置和实际终端状态。

```text
rmg session open lab --json
rmg session read SESSION_ID --json
rmg session shell-enable SESSION_ID --token CONTROL_TOKEN --confirm-posix --json
```

只有返回 `shell.state = ready` 才能继续。示例中的会话 ID、控制令牌都应替换为真实返回值。使用应用 profile 或 `session open --command` 进入的终端会被拒绝探测；必须先退出应用，并观察 profile 中配置的外层提示符。没有可靠退出判据时，可以另开一个普通 Shell 会话。

连续操作共享目录和导出的环境变量：

```text
rmg session exec SESSION_ID "cd /opt/test; export TEST_MODE=smoke" --token CONTROL_TOKEN --request-id prepare-1 --json
rmg session exec SESSION_ID "./deploy.sh" --token CONTROL_TOKEN --request-id deploy-1 --timeout 10 --json
```

可以使用已有的 `--task TASK_ID --step-id STEP_ID` 把命令关联到任务。终端请求编号和任务步骤编号分别保持不变。

返回结果中的重要字段：

| 字段 | 含义 |
| --- | --- |
| `request_id`、`id`、`session_id` | 原始请求、操作记录及所属终端 |
| `state` | `succeeded` / `failed` 是已观察到退出码；`unknown` 是没有可靠完成证据 |
| `exit_code` | 本条 Shell 命令的退出码，未知时为 `null` |
| `observation.data` | 本条命令对应的终端输出区间，最多保留 64 KiB 的展示尾部 |
| `output_range.start/end` | 本地脱敏终端日志中的绝对字节范围；可用 `session read` 进一步查看 |
| `observation.gap`、`truncated` | 日志是否缺失、展示结果是否被截断 |
| `shell_state` | 完成后 Shell 能力是否仍为 `ready`；设置变化等会变为 `lost` |

退出码为 0 表示命令退出成功。软件是否正常启动、测试是否通过，仍应执行相应业务验证。

## 超时后继续观察

等待时间是本次观察窗口，范围为 0 到 60 秒。窗口结束不会发送中断，也不会把执行判断为失败。

```text
rmg session exec-get SESSION_ID deploy-1 --json
rmg session exec SESSION_ID "./deploy.sh" --token CONTROL_TOKEN --request-id deploy-1 --timeout 30 --json
```

`exec-get` 只读取已保存的证据。再次调用 `session exec` 时，使用相同请求编号、相同命令和相同 `sensitive` 设置，可以继续等待原请求，**不会再次发送命令**。观察时间可以调整。改变命令但复用编号会返回 `request_conflict`。

未确认完成的命令会占用该终端的输入权限；其他原始输入或新 Shell 命令会被拒绝，以免发到仍在运行的程序中。如果确实需要中断：

```text
rmg session interrupt SESSION_ID --token CONTROL_TOKEN --json
```

它明确发送 Ctrl-C，并把原命令保留为未知。中断被发送不证明进程已停止。之后先观察终端，确认已经回到 Shell，再重新启用 Shell 能力。CLI 响应丢失或本地观察被取消，不会自动取消远端工作。

## 原理

每条命令有随机的开始和完成标记。发送到终端的是打印标记的 Shell 表达式，真正的标记包含控制字符，因此普通输入回显、历史提示符和上一条命令的标记不会直接命中本次完成判据。

实际命令通过当前 Shell 的 `eval` 执行，没有另开子 Shell，也没有放进条件语句改变 `set -e` 的行为，因此 `cd` 和 `export` 能影响后续命令。执行意图及输出起点在发送前写入本地记录；同一个请求的并发调用共享同一次发送和观察。

结果匹配必须同时具备本次开始帧和本次完成帧。日志保留策略造成缺口时，接口返回未知，不拼接缺口两侧的输出制造完成证据。

## 使用边界

- 支持已验证的 bash、dash、BusyBox ash 的 POSIX 命令子集。不是通用终端模拟器，也不适用于 TI、数据库前台等其他解释器。
- 原始 `session write`、`session step`、退出应用等输入会撤销 Shell 能力，避免把封装表达式发送到错误的前台。新输入被阻止时，优先观察原请求。
- `exit`、`exec`、`set -e; false` 等可能使 Shell 在发送完成帧之前退出，结果保持未知。观察到 Shell 选项或 `stty` 设置变化时，即使本条退出码已知，也会撤销后续 Shell 能力。
- 启用阶段拒绝已打开 `errexit`、`noexec`、`verbose`、`xtrace` 的 Shell。通过原始输入调整这些设置后，需要重新观察并启用。不要把此接口当成不改变任何特殊变量的透明终端：封装会影响 `$?`；跨请求请使用返回的 `exit_code`。
- 单条命令最多 1024 UTF-8 字节，封装后最多 3500 字节，以约束 PTY 行输入。较大的脚本应先上传，再执行脚本路径。
- 终端的 stdout/stderr 混合；后台进程可能把输出写入同一区间。返回值标明 `terminal_interval_may_include_background_output`，不能把区间内所有内容都归属于本条命令。`&` 启动的后台任务完成情况需要另查。
- 随机标记用于避免普通回显和历史输出误判，不是防御恶意远端程序伪造协议的安全边界。修改 `eval`、`printf`、Shell trap、提示符钩子或文件描述符也可能破坏协议。
- 本地管理进程重启或连接丢失后，原请求和已留存日志仍能查询，但原交互终端无法恢复。需要远端持久结果的工作应使用受管 `job`。

## 已执行的验证

确定性测试覆盖了发送前落盘、同编号重试、调用方取消、并发观察、回显与旧标记、ANSI/无换行/中文输出、实际非零退出码、日志缺口、分片敏感输入、断线、管理进程重启和明确中断。

WSL 中使用真实 PTY 分别验证 bash、dash、BusyBox ash，检查工作目录和环境继承，并用独立文件计数证明超时后继续观察只有一次业务执行；同时验证 `exit`、`exec`、`set -e`、`stty` 改动和延迟后台输出。CLI + TaskBook 测试验证同一任务步骤可以继续观察原请求。测试源码为 `tests/test_session_shell.py` 和 `tests/test_shell_task_cli.py`。

这些是受控环境的协议与状态验证。尚未据此证明任意真实 CTP 设备兼容或 Agent 的普遍成功率。
