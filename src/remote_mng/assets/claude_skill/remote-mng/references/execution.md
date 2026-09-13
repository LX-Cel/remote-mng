# 独立命令、传包与验收

先将 `<SKILL_BASE_DIR>`、`TARGET`、路径与 ID 替换成任务中的实际值；所有操作继续通过技能内包装脚本。

## 短命令

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json exec TARGET "uname -a" --timeout 30 --wait --wait-timeout 35
```

`exec` 使用新的远端执行通道，不继承其他交互会话的目录或临时环境。需要时提供 `--cwd /REMOTE/DIRECTORY` 与重复的 `--env NAME=VALUE`；这些包装要求目标是 POSIX shell。远端环境参数不得用于传递密码。

复杂命令放入本地 UTF-8 脚本，避免多层 Shell 引用错误：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json exec TARGET --script ./remote-check.sh --cwd /REMOTE/DIRECTORY --timeout 30 --wait --wait-timeout 35
```

脚本内容来自用户要求或项目已有操作约定，不把示例 `uname -a` 当部署验证。`--script -` 可从标准输入读取，但调用方必须提供完整内容并关闭 stdin；它传递的是待执行脚本，不是远端应用的交互式 stdin。

预计长时间运行、输出量大，或需要断开后查询时，使用 [持久作业](jobs.md)。`--timeout` 限制本次执行观察，`--wait-timeout` 只限制本地等待；两者到期都不能证明远端进程停止。

## 上传和下载

确认本地构建已经产出所需文件，记录产物版本、源路径和预期远端最终路径。本技能不要求迁移或重新实现本地构建系统。

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json file upload TARGET ./package.tar.gz /REMOTE/DIRECTORY/package.tar.gz --wait --wait-timeout 60
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json file download TARGET /REMOTE/DIRECTORY/results.log ./results.log --wait --wait-timeout 60
```

远端参数是**绝对的最终文件名或目录名**，不是自动补文件名的目录快捷方式。Windows 本地文件使用原生路径或项目相对路径；不要把远端 `/...` 改写成盘符路径。

默认使用 SFTP，拒绝覆盖已有目标。有意替换已确认的路径时增加 `--overwrite`，目录使用 `--recursive`。递归逐文件完成，不是整棵目录的原子事务；发生故障可能已有部分文件完成。

确认目标需要且支持遗留 SCP 后，可明确选择：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json file upload TARGET ./package.tar.gz /REMOTE/DIRECTORY/package.tar.gz --protocol scp --wait --wait-timeout 60
```

- 工具的 `--protocol scp` 是遗留 SCP 协议；不要与现代系统 `scp` 命令默认使用 SFTP 混淆。不会自动降级协议。
- SFTP 支持逐文件 SHA-256 内容校验；读取完成结果中的 `verification`、`sha256` 与字节数。它不替代部署后的版本或功能检查。
- 遗留 SCP 首版只支持普通文件，不支持递归，也不执行端到端内容校验，`verification` 为 `not_performed`。需要校验时按目标能力另行取得证据。
- SCP 要求 SSH、远端 SCP 程序及 POSIX shell；上传发布需要 `ln -T`，显式覆盖需要 `mv -fT`。SFTP 覆盖需要服务端 `posix-rename`。精简设备缺能力时会失败，不应通过删除原文件来自动绕过。
- 失败可能留下 `.partial` 文件；不按 partial 长度续写。SFTP 可用 `--resume --conflict skip-identical` 基于 SHA-256 跳过已完整发布且内容相同的文件，再传剩余文件。这是文件级恢复，不是字节级断点续传。先查询旧操作，确认已结束及部分文件状态再创建恢复步骤。

SFTP 目录可指定 `--recursive --concurrency 3`（1..8 个独立 SFTP 通道，共用一条 SSH 连接）。`--conflict error` 拒绝冲突；`skip-identical` 仅跳过摘要一致的目标；`overwrite` 明确替换。摘要不一致不会因 `--resume` 被自动覆盖。返回 `manifest` 和总体进度，失败清单区分 completed/failed/unknown/not_started；未确认的文件不能算完成。遗留 SCP 不支持这些批量优化选项。

## 操作记录与日志

不加 `--wait` 时会快速返回操作 `id`；即使 CLI 返回 0，JSON 状态仍可能是 `running`。保存该 ID，再查询到最终状态：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json operation get OPERATION_ID
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json operation logs OPERATION_ID --stream stdout --offset 0 --limit 65536
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json operation logs OPERATION_ID --stream stderr --offset 0 --limit 65536
```

首次读取用 `--offset 0`，后续分别使用每个流返回的 `next_offset`。`eof` 仅表示当前日志快照读尽；`log_truncated` 表示证据有缺失。避免每次从 0 重新读取全部历史；检查操作状态后继续按需读取新输出。

`succeeded`/`failed` 结合退出码或传输结果判断；`unknown`、响应丢失、超时都不能直接视为未执行。使用 `operation get` 或 `operation list` 找回原记录，不自动重发相同命令。普通操作记录属于本地管理进程，不能替代远端受管作业的恢复证据。

向用户报告时区分：产物已传完、部署命令退出、程序已启动、业务测试已通过。只有实际取得对应输出、退出码、版本或健康检查证据，才能报告那一步完成。
