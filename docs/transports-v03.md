# 0.3 连接诊断、登录路线与传输恢复

这些命令主要由 Agent 按 Skill 调用。开发者提供目标配置、经过独立核实的主机密钥和凭据引用，通过网页查看诊断与任务记录。

## 真实连接失败如何解释

错误的 `details.diagnostic` 包含 `stage`、`route`、`observed_at`、`business_input`、`evidence`、`recovery_actions`、`trace`。阶段包括 DNS 失败、TCP、代理协商、SSH 主机密钥、认证、登录节点、终端、命令与 SFTP。

- `route=jump` 表示一层跳板自身失败；`route=target` 表示最终 SSH 目标阶段失败。
- SSH 成功轨迹在真实连接的 TCP 建立、进入认证和认证完成回调记录。没有执行额外 DNS 探测来冒充本次连接的成功证据；DNS 错误来自本次连接异常。
- `business_input=not_sent` 表示本次请求的业务输入还未派发。登录节点发送的菜单选择或凭据另记为 `input_sent`，不与部署命令混淆。
- `business_input=unknown` 表示可能已派发、尚无完整结果。继续查询原资源；不能据此重放部署或测试。
- 登录超时保留失败节点、之前匹配与发送的轨迹，以及最近输出；不自动重试同一节点或同一认证凭据。
- 凭据从环境变量引用。诊断先脱敏再存储；终端跨网络包分片回显也会脱敏。最近输出最多保留 4096 字符，轨迹最多 64 条，并提供截断标记。库的原始异常文本不直接作为网络诊断返回。

`rmg target inspect lab --json` 执行只读检查；菜单或应用前台默认 `shell=unknown`，不会收到 Shell/helper 探测命令。`job_ready` 独立表示持久作业条件，连接成功不等于可以运行作业。helper 支持存储协议时额外报告空间、日志占用和限额；旧 helper 的作业仍可查询，但新维护能力需要安装当前 helper。

## 一层标准 SSH 跳板

将下列内容保存为目标配置，例如 `lab.json`，再由 Agent 执行 `rmg target add lab --file lab.json --json`。

```json
{
  "host": "board.example.internal",
  "port": 22,
  "username": "tester",
  "client_keys": ["~/.ssh/board_ed25519"],
  "known_hosts": "~/.ssh/known_hosts",
  "ssh_config": [],
  "connect_timeout": 30,
  "jump": {
    "host": "jump.example.internal",
    "port": 22,
    "username": "developer",
    "client_keys": ["~/.ssh/jump_ed25519"],
    "known_hosts": "~/.ssh/known_hosts",
    "ssh_config": []
  }
}
```

跳板需要允许标准 `direct-tcpip` 转发。跳板和目标分别进行认证与主机密钥校验；命令和 SFTP 均通过最终目标 SSH 连接执行。关闭连接时也会关闭工具拥有的跳板连接。

只支持一层显式跳板；不支持把菜单式登录当作标准 SSH 转发。可以继续使用既有 `ssh_config` 设置，但复杂配置应先通过检查确认实际路线。

## HTTP CONNECT 与 SOCKS5

直接 SSH 目标可增加以下 `proxy` 字段；`type` 可选 `http` 或 `socks5`。

```json
{
  "proxy": {
    "type": "http",
    "host": "proxy.example.internal",
    "port": 8080,
    "username_env": "RMG_PROXY_USER",
    "password_env": "RMG_PROXY_PASSWORD"
  }
}
```

无需认证时同时省略两个环境变量字段。代理和跳板组合时，代理写在 `jump.proxy`，不能同时设置目标顶层的 `jump` 与 `proxy`。当前显式代理只用于 SSH；Telnet 可另配经过代理的 SSH 传包端点。

HTTP 支持 CONNECT 和 Basic 代理认证，SOCKS5 支持无认证、用户名/密码认证及域名、IPv4、IPv6 地址格式。代理握手有总连接超时和响应大小限制；失败不降级为直连。目标 SSH 主机密钥始终验证。HTTP 代理连接本身未套 TLS，代理凭据只应交给你信任的已配置代理；目标 SSH 通道仍由 SSH 保护。尚未做企业代理、OTP 和任意堡垒机的现场兼容性验收。

## 有界登录决策树

原有顺序 `login_steps` 继续支持；复杂菜单可改用 `login_flow`。两种形式不能同时配置。以下示例根据菜单版本选择一个分支，最终观察目标提示符：

```json
{
  "host": "menu.example.internal",
  "protocol": "ssh",
  "known_hosts": "~/.ssh/known_hosts",
  "shell": "unknown",
  "login_flow": {
    "start": "menu",
    "max_steps": 8,
    "timeout": 60,
    "steps": [
      {"id": "menu", "branches": [
        {"expect": "Menu v1", "next": "old"},
        {"expect": "Menu v2", "next": "new"}
      ]},
      {"id": "old", "expect": "selection: ", "send": "1", "next": "done"},
      {"id": "new", "expect": "selection: ", "send": "2", "next": "done"},
      {"id": "done", "expect": "target> ", "finish": true}
    ]
  },
  "transfer": {
    "host": "board.example.internal",
    "port": 22,
    "username": "tester",
    "known_hosts": "~/.ssh/known_hosts",
    "client_keys": ["~/.ssh/board_ed25519"],
    "shell": "posix"
  }
}
```

配置中的提示符和选择值必须来自你确认的实际终端。工具不从终端输出猜测目标身份或凭据。密码节点用 `send_env`，普通菜单选择用 `send`；默认附加换行，可设置 `newline=false`。

上限：64 个节点、每节点 16 个分支、128 次遍历、整体 300 秒、单节点 300 秒；默认整体 60 秒、32 次遍历、每节点 15 秒。正则最多 2048 字符，每次匹配也有 CPU 时间上限。循环到达上限就失败，不继续发送。最终 `finish` 节点只观察、不发送。

菜单登录到达设备前台，不代表该 SSH 连接的 SFTP 到达同一设备。此类目标必须明确提供独立 `transfer.host`，否则在传包前返回 `transfer_endpoint_required`。普通直连 SSH 的既有 `transfer` 局部覆盖仍可沿用相同主机与连接路线。

## 配置修复：预览、版本冲突、备份

```text
rmg target snapshot lab --json
rmg target patch lab --file repair.json --revision REVISION_FROM_SNAPSHOT --json
rmg target patch lab --file repair.json --revision REVISION_FROM_SNAPSHOT --apply --json
rmg target inspect lab --json
```

`repair.json` 是 JSON merge patch。例如修复超时配置：`{"connect_timeout":30}`；值为 `null` 表示删除该字段再应用默认值。嵌套对象合并，数组整体替换。预览返回校验后的配置与字段差异，不写文件。实际应用要求配置内容仍与读取时的 revision 相同；任何并发修改返回 `config_conflict`，需要重新读取和审阅。

应用前在本地状态目录 `config-backups/` 保存完整配置备份。修改只影响后续连接，不重连现有会话；错误密钥或目标身份不能靠关闭主机校验自动“修好”。

## 目录并发与文件级恢复

```text
rmg file upload lab ./build/package /tmp/package --recursive --concurrency 3 --wait --json
rmg file upload lab ./build/package /tmp/package --recursive --concurrency 3 --resume --wait --json
rmg file download lab /tmp/results ./results --recursive --concurrency 3 --resume --wait --json
```

SFTP 并发度默认 1，上限 8。每个并发任务使用同一个 SSH 连接上的独立 SFTP 子通道；受控服务器测试验证了实际并行写入。单次清单最多 10000 个文件，过大目录应分批。远端若不能开足所需子通道，则在开始文件传输前报错并建议降低为 1；若准备阶段已创建目录，错误同时列出这些目录，不声称整个操作没有任何副作用。

| 冲突策略 | 遇到已经存在的文件 |
| --- | --- |
| `--conflict error`（默认） | 报冲突；配合 `--resume` 时先完整 SHA-256 校验，相同则跳过，不同仍报冲突 |
| `--conflict overwrite` 或原有 `--overwrite` | 允许发布替换；配合 `--resume` 时已验证相同的文件仍跳过 |
| `--conflict skip-identical` | 完整校验相同才跳过，不同报冲突 |

没有按长度拼接 `.partial` 文件的逻辑。新传输写新的随机临时文件，校验成功后才发布到正式路径；上传时源文件发生变化也会拒绝发布。`resume` 是对正式文件重新核对摘要后的文件级恢复，不是字节续传或跨设备事务。

结果包含完整 `manifest`、逐文件状态和摘要、总文件数、实际传输字节与跳过文件数。总体进度区分 `transferring`、`verifying`、`verifying_existing` 与 `completed`；`verified_bytes` 表示确认完成的内容规模，`bytes_transferred` 表示本次实际传输，不包含已经校验跳过的文件。

部分失败返回 `transfer_partial` 及每个文件的状态、失败原因。全局超时已识别的运行中文件保留为 `unknown`，未启动文件标记 `not_started`；已完成文件仍保留证据。传输完成仅表示文件发布与摘要校验完成，不表示已经部署或业务验收通过。SCP 保持原有单文件模式且明确 `verification=not_performed`，不能使用并发、摘要跳过或恢复选项。

## 已验证与未验证

新增测试使用本机受控 SSH/SFTP、TCP 代理和 Telnet 服务，验证路线落点、两端主机密钥、代理握手、分支登录、凭据分片回显、限额、文件清单与恢复。具体次数见 [协议验证记录](validation-transport-v03.md)。这些是确定性协议测试，不是 Claude Code 成功率测量；真实企业堡垒机和 CTP 设备尚未验收。
