# 目标、认证与管理进程

所有命令中的 `<SKILL_BASE_DIR>` 按 SKILL.md 替换成实际技能目录；`TARGET` 和其他大写占位符替换成已确认的实际值。文档模板不代表用户已授权这些地址或命令。

## 选择环境和已有目标

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json target list
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json server status
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json target check TARGET
```

Windows 原生、WSL 和 Linux 分别使用其自身的用户目录、SSH 配置与管理进程。默认状态目录是该环境用户的 `~/.remote-mng`；可用 `RMG_HOME` 或每次一致的绝对 `--home` 指定其他目录。进入另一个项目不会自动切换目标配置；每次明确选择目标。

`target list` 等常规调用可按需启动管理进程，`server status` 本身不启动它。若宿主限制独立子进程，出现 `daemon_start_failed`，让开发者在独立终端使用其已安装的 `rmg server start` 预启动，再继续通过本技能包装脚本调用。不要为普通远端错误反复重启管理进程，重启会断开共享的交互会话；受管作业仍保留远端记录。

## 首次没有可用目标

先取得会改变执行位置或授权边界的必要信息：目标别名、实际主机与端口、SSH/Telnet 协议、用户名或登录提示、认证引用、本地与远端路径、是否进入 POSIX shell。需要传包时还要确认 SSH 文件传输入口；需要部署时取得已有部署命令、启动方式和验收判据。

只询问缺失信息。不要要求把密码或私钥粘贴进对话。认证可引用开发者已配置的环境变量、密钥文件或 SSH agent；先准备脱敏配置，再按任务授权写入目标。

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json target add TARGET --file ./remote-target.json
```

`target add` 会**替换同名目标的完整配置**。更新前检查已有配置，保留仍需使用的认证、传输入口和动作限制；不要用不完整的新 JSON 无意清除它们。

## SSH 配置模板

下面所有 `*_FROM_USER` 与 `/LOCAL/PATH/...` 都是待替换值。端口 22 仅是 SSH 默认值，实际非默认端口应明确写入。

```json
{
  "protocol": "ssh",
  "host": "HOST_FROM_USER",
  "port": 22,
  "username": "USERNAME_FROM_USER",
  "client_keys": ["/LOCAL/PATH/id_ed25519"],
  "known_hosts": "/LOCAL/PATH/known_hosts",
  "shell": "posix",
  "helper_dir": "~/.local/share/remote-mng"
}
```

常用字段：

- `password_env`：保存密码的环境变量**名称**；不保存其值。
- `passphrase_env`：加密私钥口令的环境变量名称。
- `client_keys`：本地私钥路径列表，支持 `~`。省略时保留默认密钥和 agent 行为，空列表关闭默认私钥文件加载。
- `known_hosts`：已核验的主机密钥文件；省略时使用该运行用户的 `~/.ssh/known_hosts`。
- `ssh_config`：要读取的 OpenSSH 配置文件路径列表；只支持底层实现具备的配置项，不等于系统 OpenSSH 的全部行为。
- `encoding`、`term_type`、`cols`、`rows`：终端编码与类型、初始尺寸，默认 `utf-8`、`xterm`、80、24。
- `connect_timeout`、`transfer_timeout`：连接和传输的超时秒数。
- `allowed_actions`：可选动作范围，使用 `check`、`exec`、`session`、`transfer`、`job`；不提供时不额外限制，空列表拒绝这些目标动作。遇到拒绝先核对现有授权，不以扩大配置绕过用户意图。
- `helper_dir`：持久作业辅助程序和记录的远端目录，须为绝对 POSIX 路径或以 `~/` 开头。

SSH 默认假设 `shell: "posix"`。实际远端不是 POSIX shell 时明确设为 `unknown`，使用适合该设备的交互入口。

首次信任失败或主机密钥变化时，先通过用户、服务器控制台或管理员提供的独立渠道核验指纹，再使工具读取经核验的 `known_hosts`。不要自动关闭主机身份检查，也不要仅凭 `ssh-keyscan` 返回的密钥就接受身份。

## Telnet 配置与传包入口

```json
{
  "protocol": "telnet",
  "host": "HOST_FROM_USER",
  "port": 23,
  "shell": "unknown",
  "encoding": "utf-8",
  "login_steps": [
    {"expect": "login: $", "send": "USERNAME_FROM_USER"},
    {"expect": "Password: $", "send_env": "REMOTE_TARGET_PASSWORD"},
    {"expect": "ACTUAL_FINAL_PROMPT_REGEX", "timeout": 15}
  ],
  "transfer": {
    "host": "SSH_TRANSFER_HOST_FROM_USER",
    "port": 22,
    "username": "SSH_USERNAME_FROM_USER",
    "password_env": "REMOTE_TARGET_PASSWORD",
    "known_hosts": "/LOCAL/PATH/known_hosts"
  }
}
```

`expect` 是实际设备提示的正则表达式；上述登录提示也须按设备修改。每一步匹配前一步之后的输出，`send` 用于普通文本，秘密通过 `send_env` 引用；默认发送 CRLF，可用 `newline: false` 关闭。

Telnet 默认 `shell: "unknown"`，可进行持续交互。只有确认登录后进入 POSIX shell，才改成 `posix` 以允许独立命令和持久作业；不要向专用菜单或测试前台注入 Shell 完成标记。Telnet 独立命令的 stdout/stderr 合并。

Telnet 本身不传文件。`transfer` 单独指定 SSH 入口；明确写入它的主机、端口、用户名和认证字段，并核对文件路径是否属于预期设备。没有 SSH 文件入口时，不承诺 SCP/SFTP 传包成功。Telnet 在线传输为明文，环境变量引用不会改变协议本身。

## 凭据更新

密码、口令及 `send_env` 的值必须存在于**管理进程启动时**的环境中。另一个终端后来设置变量，不会更新已运行管理进程。需要更新时先说明共享交互会话会断开，在现有授权范围内安排重启；不要打印环境变量值或读取私钥内容来诊断。
