# 同一 POSIX Shell 内执行命令

仅在已确认到达 POSIX Shell、需要保留目录和环境时使用；CTP/TI 等应用前台继续用 session step，不发送 Shell 包装器。

下面命令中的入口、ID 和 TOKEN 均替换为真实值，每次通过完整 Skill 包装路径调用。

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session open TARGET
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session shell-enable SESSION_ID --confirm-posix --token TOKEN
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session exec SESSION_ID "cd /tmp && export TEST_MODE=trial" --request-id set-env --token TOKEN
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session exec SESSION_ID 'printf "%s\n" "$TEST_MODE"; pwd' --request-id read-env --token TOKEN
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session exec-get SESSION_ID read-env
```

每个逻辑命令使用独立 request-id。相同 ID 与相同命令不会重发；不同命令报 request_conflict。观察超时可用同一 `session exec`、同一命令与 request-id 增加 timeout 继续等，也可用 exec-get 只读查询。返回 exit_code、state、output_range 和 next_offset；输出区间可能混有后台程序内容，退出码不能代替业务判据。

每条命令最多 1024 UTF-8 字节。大型脚本上传后调用，长任务使用 job。`exit/exec/set -e`、终端设置变化、信号或断线可能导致完成标记无法取得，结果保持 unknown，Shell 需要重新确认；不能凭提示符猜退出码。

普通 session write/step 会撤销 Shell 确认，进入应用后不能直接 session exec。配置了前台 profile 的会话，要先有外层退出提示符证据再确认。已有命令未结束时普通输入被拒绝；明确需要中断时使用 `session interrupt SESSION_ID --token TOKEN`，再观察，Ctrl-C 不证明进程已停止。

同一 Shell 和去重记录依赖本地 daemon 与原连接。断线后要查询的非交互工作必须用 helper job。

普通 Shell 收尾直接调用 `session close SESSION_ID --token TOKEN`，不要先发送 `exit` 再等待额外提示符。若已经收到 EOF，close 返回 `already_disconnected` 仅确认连接已关闭，不代表远端程序已停止；无需重开会话做收尾。应用前台才根据实际 profile 使用 leave，然后 close。
