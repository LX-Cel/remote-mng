# 连接、终端与文件传输

`remote-mng` 的目标配置描述连接能力，业务命令通过 CLI 或 MCP 提交。SSH/Telnet 终端与 SSH 文件通道分别配置；交互式应用无需写入工具源码。远端辅助程序只用于持久作业，普通终端与 SFTP/SCP 传输不要求安装它。

## SSH 配置和首次信任

[target.example.json](../examples/target.example.json) 是可导入的单目标配置，示例指向本机 `127.0.0.1:2222`、用户 `demo`。使用前将地址、端口和用户名改为你的测试服务器；这个配置文件不会自行启动 SSH 服务。

```sh
rmg target add lab --file examples/target.example.json
rmg target check lab
```

所有 SSH 连接都验证主机密钥，包括文件传输使用的单独入口。未指定 `known_hosts` 时读取当前运行用户的 `~/.ssh/known_hosts`；文件缺失、主机未收录或密钥变化都会失败，不提供自动关闭验证的参数。

首次连接可以使用系统 OpenSSH 建立信任：

```sh
ssh -p 2222 demo@127.0.0.1 exit
```

在接受首次显示的指纹前，通过服务器控制台或管理员提供的独立渠道核对。例如，服务器管理员可用 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` 查看该主机密钥指纹。确认后 OpenSSH 会写入自己的 `known_hosts`。工具必须读取同一份文件；使用非默认文件时也要在目标配置中指定对应路径。`ssh-keyscan` 只能获取网络上返回的密钥，单独运行它不能确认远端身份。主机重装后密钥变化，需要先核实变更原因再更新记录。

认证通过以下配置字段选择：

| 字段 | 含义 |
| --- | --- |
| `client_keys` | 私钥文件路径列表，支持 `~`。省略时保留 AsyncSSH 默认密钥与 agent 行为；空列表关闭默认私钥文件加载。 |
| `password_env` | 保存密码的环境变量名。配置保存变量名，不保存密码值。 |
| `passphrase_env` | 加密私钥口令的环境变量名。 |
| `ssh_config` | 要读取的 OpenSSH 配置文件路径列表，支持 AsyncSSH 实现的配置项。不是对系统 OpenSSH 客户端行为的完整复刻。 |
| `known_hosts` | 经核验的主机密钥文件路径。 |

凭据环境变量必须存在于**本地会话管理进程启动时的环境中**。该进程已经运行时，修改另一个终端的环境变量不会更新它；先设置凭据，再重启 `rmg server`。交互式会话不会在本地管理进程重启后保留，持久作业仍可通过远端记录查询。

Windows 路径在 JSON 中可以使用正斜杠，例如 `C:/Users/demo/.ssh/id_ed25519`。WSL 使用其 Linux 用户的路径、SSH agent 和配置；Windows 原生与 WSL 的用户目录及 daemon 相互独立。

## Telnet 登录和应用交互

下面的目标配置使用逐步匹配完成登录。`expect` 是 Python 正则表达式；`send` 用于普通文本，密码使用 `send_env`。每一步只检查前一步之后收到的输出；不要把所有提示写成一个宽泛的匹配条件。

```json
{
  "protocol": "telnet",
  "host": "192.0.2.10",
  "port": 23,
  "encoding": "utf-8",
  "shell": "posix",
  "login_steps": [
    {"expect": "login: $", "send": "demo"},
    {"expect": "Password: $", "send_env": "RMG_TARGET_PASSWORD"},
    {"expect": "(?m)^[^\\r\\n]*[#$] $", "timeout": 15}
  ],
  "transfer": {
    "host": "192.0.2.10",
    "port": 22,
    "username": "demo",
    "password_env": "RMG_TARGET_PASSWORD",
    "known_hosts": "~/.ssh/known_hosts"
  }
}
```

`192.0.2.10` 是文档示例地址。应将最后一条提示符规则改成实际设备的提示；它只说明观察到对应文本，不能证明业务任务成功。发送内容默认追加 CRLF，设置 `newline: false` 可关闭追加。

只有确实登录到 POSIX shell 的目标才设置 `shell: "posix"`。Telnet 的默认值是 `unknown`，仍可使用持续交互终端，但会拒绝 shell 命令包装和辅助程序安装。登录后直接进入专用菜单、测试前台或设备控制台的目标应保持 `unknown`。

Telnet 的独立命令执行使用一个新连接，发送随机完成标记并读取退出码。完整标记不出现在发送的包装命令中，终端回显不会被当作已完成。它的标准输出与标准错误合并，不能像 SSH exec 通道那样分别获取。超时、掉线和缺失退出码表示结果未知，不自动重发命令。SSH/Telnet 会话都支持连续读写和控制字符；SSH 支持动态调整终端尺寸，Telnet 首版只支持连接时设置 `cols`/`rows`。

独立命令的输出观察上限为 **16 MiB**，以解码后文本的 UTF-8 字节数计算。SSH 同时读取 stdout 和 stderr，并共用这个总上限，同时仍支持向 stdin 输入文本；Telnet 的合并终端输出使用相同上限。超限返回 `output_limit` 和 `outcome: "unknown"`，关闭本次观察连接，不宣称已经终止远端进程。预计产生大量输出的任务应使用 `job`，再通过带游标的日志读取获取结果。

Telnet 文本 stdin 使用带随机分隔符的 heredoc，仅接受不含 NUL、以 LF 结尾的文本或空字符串。它不提供二进制 stdin 保证。Telnet 本身是明文协议，适合已经隔离或另有加密通道的设备网络；保存环境变量引用不会改变线上协议的明文性质。

## 通用交互演示

可以在任意具有 POSIX shell 的测试目标准备一个 `demo-ti.sh`，模拟有提示符的测试程序：

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

上传到选定的测试路径：

```sh
rmg file upload lab ./demo-ti.sh /tmp/demo-ti.sh --wait
```

准备本地 `demo-profile.json`：

```json
{
  "name": "generic-test-console",
  "enter": "sh /tmp/demo-ti.sh",
  "prompt": "demo> $",
  "exit": "quit",
  "timeout": 10
}
```

打开会话后，使用返回的 session ID 和 `control_token` 替换下面的大写占位符：

```sh
rmg session open lab --profile-file demo-profile.json
rmg session write SESSION_ID ping --newline --token CONTROL_TOKEN
rmg session read SESSION_ID
rmg session leave SESSION_ID --token CONTROL_TOKEN
rmg session close SESSION_ID --token CONTROL_TOKEN
```

自动化等待必须使用写入前的输出游标，避免把上一次 `PONG` 当作本次结果。人工可以通过 `rmg session attach SESSION_ID --force` 明确接管，原来的写入令牌随即失效；`Ctrl+]` 脱离本地接管界面，不等同于向远端程序发送退出命令。退出前台是否影响后台程序，由具体应用决定。

## 文件传输与覆盖语义

远端路径必须是绝对路径，并且表示**最终精确路径**。例如 `/tmp/package.tar.gz` 是最终文件名，不是自动拼接源文件名的目录快捷方式。目录递归也使用最终目录名。文件名中的空格、中文和引号受支持；远端路径中的 NUL、CR、LF 被拒绝。

```sh
rmg file upload lab ./package.tar.gz /tmp/package.tar.gz --wait
rmg file download lab /tmp/results.log ./results.log --wait
rmg file upload lab ./package.tar.gz /tmp/package.tar.gz --protocol scp --overwrite --wait
rmg file upload lab ./build /tmp/build --recursive --wait
```

| 能力 | SFTP（默认） | 遗留 SCP（`--protocol scp`） |
| --- | --- | --- |
| 普通文件上传/下载 | 支持 | 支持 |
| 目录递归 | 支持；逐文件复制 | 不支持；先打包成归档再传 |
| 防止默认覆盖 | 支持 | 支持 |
| 显式覆盖 | 服务端需支持 `posix-rename` 扩展 | 远端需支持 `mv -fT` |
| 内容验证 | SHA-256，写入/下载后重新读取验证 | 未进行端到端内容校验，结果标为 `not_performed` |
| 所需远端能力 | SSH + SFTP 子系统 | SSH + SCP 程序 + POSIX shell；上传需要 `ln -T` 或 `mv -fT` |

工具内的 `scp` 明确使用遗留 SCP 协议。系统 OpenSSH 的 `scp` 命令自 9.0 起默认使用 SFTP，这两者不要混淆。不进行隐式协议降级。

文件先写入同目录下带随机后缀的 `.partial` 临时文件，再发布到最终路径。SFTP 通过协议的无覆盖 rename 或 `posix-rename` 发布。SCP 默认使用 `ln -T` 原子地拒绝已存在的目标，显式覆盖使用 `mv -fT`；`-T` 防止并发出现的目录把“写入这个精确路径”改成“放进这个目录”。远端缺少相应选项或硬链接能力时明确失败，不退化为先删除目标再复制。

正常上传/下载拒绝替换目录与符号链接，递归操作拒绝符号链接及特殊文件。服务端提供的目录条目不能包含路径穿越或 Windows 设备名。目录递归不是整个目录的事务：取消或失败时可能已有部分文件完成；显式覆盖是合并文件，不删除源中不存在的目标文件。父目录在执行期间应由受信任的用户维护；本工具不提供远端文件系统隔离。

传输操作可查询字节进度和阶段：`transferring`、SFTP 的 `verifying`、以及单文件 `completed`。递归时进度按当前 `path` 报告，不能把它当作整棵目录的百分比。总长度未知时返回 `null`。网络断开或取消可能留下 `.partial` 文件；最终结果不能确认时返回未知状态，下一次传输不会自动假定之前失败。首版没有文件断点续传。

SFTP 的 SHA-256 验证需要重新读取内容，增加网络流量；它确认传输产物的一致性，不构成恶意远端的可信证明，也不替代部署后的程序版本与功能检查。

## 已验证范围

开发环境使用 Python 3.12、AsyncSSH 2.24.0、telnetlib3 4.0.5。`tests/test_transports.py` 使用真正的 TCP/SSH/Telnet/SFTP/SCP 协议，服务端在 loopback 上运行；不是用模拟返回值代替协议层。

已验证：严格主机密钥检查、SSH 独立输出与退出码、连接中断/超时、持续终端、Telnet 分片密码回显脱敏、登录超时、随机完成标记与文本 stdin、中文/空格/引号路径、SFTP 递归和 SHA-256 校验、SCP 路径注入与恶意文件名、发布目录竞态、服务器卡住时的传输取消，以及辅助程序经 SSH/Telnet 安装、提交后断开连接，再用新连接查询真实退出码与持久日志。

Windows 原生运行不含 `/bin/sh` 的测试；需要远端 Linux 语义的用例在 WSL Ubuntu 内完整执行。尚未取得真实公司的服务器/嵌入式目标板、具体 BusyBox 固件、实际交互前台或 Telnet 脱敏记录，因此不宣称已验证这些设备。服务端脚本、可用命令、字符编码和提示符仍需目标环境试用确认。

参考：

- [AsyncSSH 官方文档](https://asyncssh.readthedocs.io/en/latest/)：连接验证、PTY、SFTP 和 SCP。
- [telnetlib3 客户端 API](https://telnetlib3.readthedocs.io/en/latest/api/client.html)：登录流、编码与终端选项。
- [OpenSSH scp 手册](https://man.openbsd.org/scp)：系统 `scp` 的默认协议与遗留模式。
- [OpenSSH ssh-keyscan 手册](https://man.openbsd.org/ssh-keyscan)：主机密钥收集及其验证边界。
