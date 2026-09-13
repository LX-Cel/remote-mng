# 持久作业、重连查询与取消

使用场景：非交互的远端构建、部署脚本、测试或其他长任务，需要在 CLI、Claude 会话或本地管理进程退出后继续查询。先将 `<SKILL_BASE_DIR>`、`TARGET`、路径及 `JOB_ID` 换成实际值。

## 能力和前置条件

受管作业要求目标具有 POSIX shell、Linux `/proc`（含 boot ID 与进程身份信息）、`setsid` 及常见 Shell 工具。辅助程序不依赖远端 Python/Node，不新增监听端口，在用户目录按需执行。Telnet 目标还须已确认 `shell: "posix"`。

**首次向一个目标提交作业前，先检查辅助程序；不要通过 `job start` 探测是否已安装。**

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json target inspect TARGET
```

检查 `checks` 中 `helper` 和 `job_dependencies` 的 `state` 是否为 `pass`。`warning`、`not_checked` 或连接未确认都不是通过。若缺少辅助程序、本次工作确实需要持久作业，且安装已在当前任务授权范围内，完成安装后再次检查；已安装且检查通过的目标直接复用：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json helper install TARGET
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json target inspect TARGET
```

安装位置由目标 `helper_dir` 决定，默认 `~/.local/share/remote-mng`，选择私有、可写的持久存储目录。安装会检查目标能力并原子替换辅助脚本，保留现有作业记录。不要为错误结果自动安装系统包或更改服务器服务策略。

## 提交前确定稳定 ID

先为**一个逻辑任务**确定唯一 `JOB_ID` 并保存。ID 为 1–64 个 ASCII 字母、数字、`.`、`_` 或 `-`，首字符必须是字母或数字。可以使用用户已有发布/测试标识，或生成新的唯一标识；不要把文档中的 `JOB_ID` 字面量用于不同任务。

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json job start TARGET --script ./remote-task.sh --cwd /REMOTE/DIRECTORY --job-id JOB_ID
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json job status TARGET JOB_ID
```

`--script` 是本地 UTF-8 脚本，内容通过远端 Shell 执行；可用 `--script -` 提供完整脚本并关闭 stdin，也可把短命令作为一个引用好的位置参数。`--cwd` 与重复的 `--env NAME=VALUE` 显式设置本次环境，不继承交互会话。

保存 `target`、`job_id` 与返回的 `operation_id`。同一 ID 和同一命令/目录/环境只对应已有作业；内容不同会报冲突，已完成 ID 也不会重跑。新逻辑任务用新 ID；未知提交继续用原 ID 查询，不能换 ID 重试。

## 读取状态与日志

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json job status TARGET JOB_ID
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json job logs TARGET JOB_ID --stream stdout --offset 0 --limit 65536
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json job logs TARGET JOB_ID --stream stderr --offset 0 --limit 65536
```

首次读取使用 0，后续对每个流分别保存并使用 `next_offset`。偏移单位为字节，`eof` 只说明当前快照读尽，不能视为作业结束。需要精确字节时使用 `data_base64`；用户入口可能遮罩已知凭据，不能把它宣称为未经修改的原始证据。文本按目标 `encoding` 解码，任意字节分页可能切开多字节字符。

状态语义：

- `running`：监督进程的身份仍得到核实；不代表业务正常或应用健康。
- `succeeded` / `failed`：已取得持久退出记录；结合 `exit_code`、日志与业务判据判断。
- `unknown`：缺少足够证据，例如远端重启、进程身份变化或退出记录丢失；不能推断“未执行”。

提交响应丢失时检查错误详情中的 `job_id`、`operation_id`，继续 `job status`。忘记标识时，可查询：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json job list TARGET
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json operation list
```

只有核对正确目标与记录后才能继续跟踪。若出现 `job_not_found`，先检查目标、运行环境、状态目录与远端记录是否仍在；响应丢失或记录不见不能证明原命令从未执行。查询不会自动恢复或重放未知任务。

按 `error.code`（或任务步骤的 `error.code`）区分恢复路径：

- `helper_not_installed`：工具确认辅助程序缺失、此次作业命令未执行。按上面的授权范围安装并复查后，可以用**相同** task ID、step ID、job ID 和参数继续；这是工具允许的确定拒绝恢复分支。
- `job_id_conflict`：该 ID 已对应不同请求。不能把旧作业的结果当成本次成功，也不能接管或取消旧作业。先核对记录；确属新的工作时使用新的步骤与作业 ID，保留冲突记录。
- `unknown`、超时、断线或响应丢失：只能用原 ID 查询状态和日志，不能反复执行原 `job start` 或 `task invoke`。同一 step 的重复返回可能只是原来的未知记录，并不是新尝试。
- `job_not_found`：仅表示查询不到记录，不等于未执行；按上文检查目标与存储，不能套用 `helper_not_installed` 的重试规则。

查询后仍无法取得执行或未执行的证据时，报告任务暂时阻塞、已知 ID、最后确认状态及缺少的证据，停止重复提交。不要通过换 ID、改 step ID 或循环原命令来“恢复”结果未知的作业。

`job start --wait --wait-timeout SECONDS` 可有限等待，但 CLI 等待超时或 Ctrl-C 只停止本地等待。需要继续时查询已有作业，不重复提交。

## 取消与验收

取消本身属于远端操作，按用户当前任务意图执行：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json job cancel TARGET JOB_ID
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json job status TARGET JOB_ID
```

工具核对 boot ID、PID 启动时间、进程组与会话身份后发送 TERM；身份不明会拒绝。`cancel_requested` 不代表已终止，忽略或处理 TERM 的任务可能仍在运行；最终结果以之后的状态和退出记录为准。不要为取得“已取消”状态而对猜测的 PID 追加 kill 命令。

退出码 0 只表示受管 Shell 的结果，部署后还需核对实际程序版本、服务状态或测试输出。报告保留可查询的作业 ID，并说明未确认的步骤。

## 恢复范围

- 支持在远端主机与存储仍正常、服务器允许脱离登录会话的进程继续运行时，跨连接和本地管理进程重启查询。
- 不提供交互式 stdin 或 PTY；需要持续前台输入的程序使用 `session`。不把既有任意进程变成受管作业。
- 不管理脚本自行脱离进程组的后台程序，不自动重启远端重启后中断的任务，不承诺断电或存储故障时记录完整保留。
- 原始请求脚本、环境和日志保存在远端用户目录，不自动轮转或清理。避免在请求中写入凭据；按项目要求安排留存与清理，不能以日志空间不足为由擅自删除其他文件。
