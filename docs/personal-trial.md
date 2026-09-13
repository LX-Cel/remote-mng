# 0.3 个人试用指南

这次试用的目标是：**你描述工作，Agent 可靠执行；你随时能看懂设备和任务的真实状态。** 先用一台自己的测试设备、一份可识别版本的产物和一条已知验证流程开始。工具不提供团队账号、共享排队或团队发布流程。

## 1. 安装好后，只需给 Agent 任务

按 [安装指南](distribution.md) 安装工具和 Skill。独立包包含运行时，安装入口会检查环境并预启动本地管理器；源码安装使用 `rmg setup --start-daemon`。Windows 在独立 PowerShell 完成这一步，之后 Claude Code 通过已安装的 Skill 使用工具。若宿主拒绝创建独立后台进程，Agent 会明确报告此前置条件，不能反复尝试绕过。在业务仓库开启新的 Claude Code 会话，先给一个简单任务：

> 使用 remote-mng，检查本地工具和我的 lab 测试设备，打开本地控制台。先告诉我连接、传包通道和长作业 helper 是否满足要求。

这会让 Agent 使用 Skill 和 CLI，开发者在网页看结果。`rmg ui` 打开本地页面；需要只返回链接而不启动浏览器时用 `rmg ui --no-open --json`。网页链接含本机访问凭证，不应放到共享文档或截图中传播。

如果没有设备配置，先补齐目标别名、地址、端口、认证引用和所需能力。以下是 SSH 配置的示例，`192.0.2.10` 是文档地址，必须换成真实设备：

```json
{
  "protocol": "ssh",
  "host": "192.0.2.10",
  "port": 22,
  "username": "developer",
  "client_keys": ["~/.ssh/id_ed25519"],
  "known_hosts": "~/.ssh/known_hosts",
  "shell": "posix",
  "helper_dir": "~/.local/share/remote-mng"
}
```

让 Agent 用 `target add lab --file ...` 导入。先核验 SSH 主机密钥，密码使用本地环境变量引用；不把私钥内容或口令交给模型。Telnet 的登录过程和独立传包入口见 [连接指南](transports.md)。没有 SSH 文件通道的纯 Telnet 设备不能直接用 SCP 传包。

## 2. 把第一次任务说清楚

提供产物位置、目标、部署脚本和可检验的成功条件。例如：

> 这次使用 build/package.tar.gz，上传到 lab 的 /tmp/package.tar.gz。按照 scripts/deploy-and-check.sh 部署；验证脚本退出码应为 0，并输出实际版本与测试汇总。请创建个人任务，记录产物摘要和每一步结果。结果不明时先查原任务，不要再次部署。

你继续使用原来的构建系统，由 Claude 在本地修改代码、构建和出包。remote-mng 管理远端执行；它不会猜测构建命令、部署路径或业务成功标准。

Agent 的典型步骤是：

1. 读取配置并做所需的只读诊断，确认待上传文件和摘要。
2. 创建稳定任务 ID，为上传、部署、交互验证选择固定步骤 ID。
3. 上传并检查传输结果；若 helper 未安装且本次需要，安装对应 helper。
4. 用受管 `job` 执行长部署或长测试，保存 job ID 并查询退出状态、读取日志。
5. 需要前台交互时用 `session step` 记录一次输入及观察，不盲目重复输入。
6. 提交结束后 seal 任务，结合真实业务证据汇报结果。

首次试用先选择你了解的流程。工具的成功率改进依赖明确的目标和判据，而不是增加未经验证的自动判断。

## 3. 从网页看哪里出了问题

控制台与 CLI 读取同一个本地管理进程和状态目录：

| 区域 | 关注内容 | 可用操作 |
| --- | --- | --- |
| 待关注与设备 | 最近检查时间、失败阶段、业务是否派发、缺失条件 | 按需检查设备、查看证据、复制恢复描述给 Agent |
| 任务 | 目标、产物、当前状态、步骤数 | 选择任务查看详情 |
| 任务详情与存储 | 每步结果、观察时间、最后确认状态、空间及日志限额 | 查询关联远端作业、请求取消受管任务、预览并清理本任务已结束作业的日志 |
| 活动与会话 | 操作记录和普通终端连接状态 | 选择对应日志 |
| 输出观察 | stdout/stderr、新输出和日志缺口 | 读取下一页或持续读取 |

页面自动刷新本地记录，不在后台轮询每台设备。远端设备检查和任务状态查询按需触发。选择远端 job 日志后，读取日志会访问该目标；持续读取不是继续执行业务命令。

状态的含义要区分：

- **进行中**：仍有未完成步骤，或 Agent 尚未结束步骤提交。持续时间长不等于失败。
- **待确认**：网络、输出或执行结果不足以确认状态。详情保留最后一次确认的状态，并显示本次观察时间；本次查询时间不等于旧状态刚刚得到确认。
- **失败**：已有对应失败证据，例如脚本返回非零退出码；仍需查看具体原因。
- **步骤完成**：工具记录的步骤完成。它不会把“出现提示符”或“上传完成”推断为业务测试通过。
- **取消请求**：记录 `cancel_requested=true`，不保证按钮返回时进程已停止。helper 的最终状态仍按退出证据显示成功或失败；不能仅凭请求已发送就报告“已取消”。

网页不是任意命令终端。它的“取消受管任务”只处理该任务自己提交的 durable jobs，不终止普通 exec、会话或其他后台业务；进一步调试仍交给 Claude 使用 CLI。

## 4. 让 Agent 复用你的个人配方

在业务仓库根目录保存下面的 `rmg-project.json`，或复制 [完整模板](../examples/rmg-project.json)：

```json
{
  "version": 1,
  "name": "个人设备部署与验证",
  "target": "lab",
  "steps": [
    {
      "id": "upload",
      "kind": "upload",
      "source": "build/package.tar.gz",
      "destination": "/tmp/package.tar.gz",
      "protocol": "sftp",
      "overwrite": false
    },
    {
      "id": "deploy-and-check",
      "kind": "job",
      "script": "scripts/deploy-and-check.sh",
      "cwd": "/tmp"
    }
  ]
}
```

源文件路径相对于配方所在目录，必须留在该项目目录内；远端路径是实际 POSIX 路径。模板不附带你的业务产物和脚本，缺少它们时 inspect 会明确失败。`overwrite: false` 表示不覆盖已有产物，后续试验请使用不同路径或根据实际需要明确允许覆盖。

让 Agent 执行 `rmg project inspect --file ./rmg-project.json --json`。这一步只校验配方、读取文件并计算摘要，输出 `method` 和 `params`，**不会构建、上传或执行脚本**。Agent 检查参数后逐步调用：

```sh
rmg task invoke trial-001 --method transfer.start --params-file ./upload-params.json --step-id upload --json
```

这里 `upload-params.json` 是 inspect 返回的该步骤 `params` 对象，不是整个配方；也可用 `--params-file -` 从 stdin 输入 JSON。`trial-001` 需事先创建。每个步骤完成必要观察后再执行下一步，不是把整个配方交给工具一次性自动运行。

个人配方只减少重复说明，不替代脚本评审、设备配置或成功判据。下次独立试验使用新任务 ID，配方中的步骤 ID 可以在新任务内复用。

## 5. 断线、未知结果和日志轮转

**先找回原记录，不急着重做。** 让 Claude 查询原 task/job/request ID，必要时读取日志。相同任务步骤与相同参数不会重复派发；参数不一致会报冲突。

一个明确的例外是 `helper_not_installed/helper_upgrade_required`：工具确认该次请求因辅助程序缺失或能力不足而未执行，安装后可复用原步骤和 job ID 再次发起。其他未知错误不会因此自动获准重试；查询不到作业也不证明它从未执行。

对于交互输入，`session step` 记录 session ID 和 request ID。响应超时后重复原请求仅继续观察，不再发送输入；匹配条件与原输入应保持一致。`session step-get` 是纯读取。匹配到提示符只证明观察到了文本，还需结合业务输出。

普通 POSIX Shell 中需要保留目录、环境并取得逐条退出码时，Agent 使用 [同一 Shell 命令](shell-v03.md)。它与 CTP/TI 应用前台分开：只有明确确认当前 Shell 后才发送命令包装器。普通 Shell 收尾直接关闭会话，无须重复发送退出指令。

本地输出流超过保留容量后，会淘汰较早内容并持续保留新输出。读取旧游标时出现 `gap=true`，同时给出 `base_offset` 和新的 `next_offset`。这表示历史证据不完整，不能当成完整日志；后续仍可正常观察新输出。默认每流最多 64 MiB，可在管理进程启动前设置 `RMG_MAX_LOG_BYTES`。

普通 SSH/Telnet 会话断线或管理进程重启后，历史可读但原前台不能自动恢复。远端 durable job 可以用原 ID 重连查询，但远端系统重启不意味着作业自动续跑。普通 `exec` 只有在执行返回后才写入捕获的日志，不适合需要实时输出的长任务。

0.3 的远端 helper 另外提供每作业每流日志上限（默认 16 MiB）、空间检查、持续轮转和终态日志清理。旧 helper 在新提交前会提示升级，旧作业仍可查询；Agent 可通过 health 查看版本和存储状态。清理先预览范围，再按计划执行，只删明确选中的已结束作业日志，并保留 ID、退出证据及日志已删除标记。详情见 [helper 存储](helper-storage-v03.md)。

若脚本已退出但后台程序仍占用 stdout/stderr，会显示 `waiting_for_output_close` 和直接脚本退出码，直到输出关闭才完成采集。不要因此再部署一次；部署脚本应按业务需求明确后台服务的输出去向。

## 6. 升级和记录试用结果

升级前先看活动会话与作业，保留 ID；在普通终端可断开的时机重启管理进程。更新 CLI 和 Skill 本身不会热更新旧 daemon。Skill 若被自己改过，安装器会拒绝覆盖；先保留修改再处理，详见 [升级和完整性保护](claude-skill.md#升级旧版本)。

第一轮试用最值得记录的是：目标和产物、任务 ID、是否完成、是否有重复输入、人工介入原因、断线后是否找回原任务，以及从网页定位问题花了多久。不要把某次模拟验收直接当作你自己的设备已经兼容。

本版实测条件和结果见 [0.3 验证记录](v03-validation.md) 与 [Claude Code 实测](validation-agent-v03.md)。[0.2 验证记录](v02-validation.md)、旧版的 [Skill 验证](claude-skill-validation.md) 与 [CLI 验证](claude-code-validation.md) 保留原有时间和范围。
