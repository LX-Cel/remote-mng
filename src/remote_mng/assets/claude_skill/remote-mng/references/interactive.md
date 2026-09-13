# 持续交互、输出观察与控制权

先将 `<SKILL_BASE_DIR>`、`TARGET`、路径和返回值占位符替换成实际值。Agent 默认通过 CLI 的 `open/write/read/wait` 操作，不进入需要真实 TTY 的 `session attach`。

## 打开会话与交互配置

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session open TARGET
```

这会建立持续的 SSH/Telnet 终端。保存返回的 `id`、`control_token`、输出游标。每次 CLI 退出不关闭该终端，只要本地管理进程和远端连接仍存活即可继续操作。

已有程序入口可使用 `--command "REMOTE_PROGRAM"`；需要自动进入、等待提示符与退出时，准备本地 `interactive-profile.json`，把以下值改成实际应用约定：

```json
{
  "name": "interactive-console",
  "enter": "REMOTE_APPLICATION_COMMAND",
  "prompt": "ACTUAL_APPLICATION_PROMPT_REGEX",
  "exit": "ACTUAL_EXIT_COMMAND",
  "exit_prompt": "ACTUAL_OUTER_PROMPT_REGEX",
  "timeout": 15
}
```

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session open TARGET --profile-file ./interactive-profile.json
```

这些模式是正则表达式，只说明观察到对应文本；工具不理解应用自己的业务成功条件。配置必须来自实际程序，不假定所有会话都是 Shell，也不内置特定测试命令或端口。没有配置退出命令时，不使用 `session leave`；按应用约定通过 `write` 发送明确的退出输入。

## 写入后只观察本次的新输出

下面的 `INPUT_TEXT` 和 `EXPECTED_TEXT` 来自当前任务；`SESSION_ID`、`CONTROL_TOKEN`、`WRITE_CURSOR` 都是实际返回值。

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session write SESSION_ID "INPUT_TEXT" --newline --token CONTROL_TOKEN
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session wait SESSION_ID "EXPECTED_TEXT" --offset WRITE_CURSOR --timeout 15
```

`WRITE_CURSOR` 必须取自**刚才那次 write 响应中的 `cursor`**。不要用上一次测试的起点，也不要将 `next_offset` 当成写入前游标，否则可能漏掉快速输出。对 `matched` 和返回的 `data` 做判断，不能只看 CLI 是否返回 0。

`wait` 默认按文本匹配；需要正则时加 `--regex`。单次观察范围为 0–60 秒，更长观察分次进行。若模式可能跨分片，下一次观察保留必要的重叠上下文，但最早边界不能早于本次 write 的游标，以免匹配历史结果。

文本匹配区分大小写，不能直接套用 profile 中的正则转义。例如观察字面 shell 提示符可用 `session wait SESSION_ID 'LAB$ ' --offset WRITE_CURSOR --timeout 10`，不要在默认文本模式中写成 `LAB\$`。未匹配时先读返回的 `data` 或增量输出，再修正观察条件；不要因此重发刚才的输入。

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session read SESSION_ID --offset NEXT_OFFSET --limit 65536
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session get SESSION_ID
```

普通增量读取使用上一页的 `next_offset`。输出按字节寻址，不能用显示字符数自行计算游标。日志截断时，指出证据缺口。`input_sent` 不代表命令完成；命中提示符、静默或 `matched: false` 不等于业务通过或远端失败。

## 输入文件、秘密与控制键

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session write SESSION_ID --data-file ./console-input.txt --newline --token CONTROL_TOKEN
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session write SESSION_ID --key ctrl-c --token CONTROL_TOKEN
```

文本、`--data-file`、`--key` 三者选一。支持的键包括 `ctrl-c`、`ctrl-d`、`escape`、`enter`、`tab`。发送 Ctrl-C 只是输入，之后仍须观察程序状态。

敏感输入可从已授权的受保护来源传到 `--data-file - --sensitive`，必须提供 EOF；不要把秘密值写在命令参数或用户总结里。`--sensitive` 注册已知输入以便输出脱敏，不是任意秘密的自动识别器。控制令牌也可通过 `RMG_CONTROL_TOKEN` 提供；该变量在每次客户端调用中生效，不要把它与管理进程启动时读取的远端密码变量混淆。

## 接管、交还和结束

同一会话只有一个写入者。`open` 直接返回控制令牌；已释放的会话可重新领取：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session claim SESSION_ID
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session release SESSION_ID --token CONTROL_TOKEN
```

控制权已被持有时，先检查是否发生人工接管。只有任务已经明确要求接管时才使用 `claim --force` 撤销旧令牌，不自动夺回他人的会话。取得新令牌后先读状态与输出，再决定是否继续先前步骤。

开发者在真实终端可执行 `session attach SESSION_ID --force` 接续同一会话；`Ctrl+]` 脱离本地界面并释放写入权，不向远端发送应用退出命令。Agent 自己继续用非交互 CLI。

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session leave SESSION_ID --token CONTROL_TOKEN
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session close SESSION_ID --token CONTROL_TOKEN
```

`leave` 按配置退出应用，`release` 仅交还写入权，`close` 关闭终端连接。根据用户任务选择需要的动作，不关闭他人会话；前台退出是否影响后台由实际应用决定，关闭终端不证明后台进程已停止。

有退出 profile 时优先调用一次 `leave`，它会发送配置中的退出命令。若已经通过 `write` 退出且看到外层提示符，直接 `close` 或 `release`，**不要再调用 `leave`**；否则同一退出命令会落到外层 shell。退出输入已发出但观察超时时，先读会话当前输出，不能用再次 `leave` 作为检查状态的方法。

本地管理进程重启或连接断开后，历史日志仍可用于分析，但旧终端不可恢复。不要把重新打开的终端当原会话，也不要重放最后一次结果未知的输入。
