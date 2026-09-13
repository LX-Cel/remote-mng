# helper 日志与存储维护

0.3 的 helper 仍是安装在目标用户目录的 POSIX Shell 脚本，不依赖远端 Python/Node，不监听端口。客户端将随包存储扩展合并到一个 `job-helper-v1.sh` 再安装。基础作业协议保持 1，新增 `storage_protocol=1`；旧作业的状态与日志接口兼容。

## 安装与升级

```sh
rmg target inspect lab --json
rmg helper install lab --json
rmg helper health lab --json
```

只对用户指定且已授权的目标执行。安装探测命令能力、检查目标目录并原子替换脚本，保留已有 jobs。新任务提交前验证存储协议，旧 helper 返回 `helper_upgrade_required`，此次业务未执行；升级后复用原 task/step/job ID 与参数继续。旧作业查询和取消不要求先升级。

配置 `helper_dir` 指向私有可写持久目录；默认 `~/.local/share/remote-mng`。目标配置可设置 `helper_max_log_bytes`（每个作业的 stdout、stderr 各自默认 16 MiB，4 KiB..1 GiB）和 `helper_min_free_bytes`（启动前默认要求 8 MiB）。变更配额只作用于新作业，不自动改变已运行作业。

`helper health` 返回可用空间、helper 作业目录占用、协议与配置阈值。新作业空间不足时返回 `insufficient_space`，不创建业务作业。相同已有 job ID 的查询不会因空间不足变成新的提交。

## 日志轮转与证据

监督进程通过 FIFO 持续排空 stdout/stderr，输出写入带绝对偏移的分块文件和原子索引；超过每流上限时淘汰旧块。保留量可能少于限额一个块，磁盘瞬时占用可能多出一个待发布块。持续读取不会因日志满额而主动堵塞业务。存储写入失败时尽力排空并标记 `log_incomplete`。

```sh
rmg job logs lab JOB_ID --stream stdout --offset 0 --limit 65536 --json
```

每个流独立保存 `next_offset`。旧游标会夹到可用起点，并返回 `gap=true`、`base_offset`；跨页发生轮转时不会把不连续输出拼接成连续证据。`eof` 只是快照读尽，结束以 job status 为准。日志缺口、`log_incomplete` 或 `logs_deleted` 不代表业务成功。

限额针对单作业输出保留。它不是磁盘预留、全系统配额或总作业数上限：并发任务、请求元数据以及业务自行写入的文件仍可能占满设备。需要按实际设备空间观察和清理。

## 清理已结束作业的日志

```sh
rmg helper cleanup lab JOB_ONE JOB_TWO --json
rmg helper cleanup lab JOB_ONE JOB_TWO --apply --plan-id PLAN_FROM_PREVIEW --json
```

默认只预览，最多明确指定 100 个 ID。计划绑定目标、范围和终态证据；应用时重新核对，计划改变、活动/未知状态或链接异常会拒绝。只删除这些作业的日志块，保留 ID、请求摘要、脚本、状态及退出记录，因此清理后同一 job ID 仍不会重复执行。日志不可恢复。

网页中选中任务后点击“预览日志清理”，只列入该任务实际受理的 job.start 作业；观察过的外部作业、ID 冲突的作业不纳入范围。确认后按同一计划清理。旧游标随后仍能查询，但明确显示日志已删除。

清理不能替代部署回滚，也不会删除业务包或结束后台应用。没有全局自动清扫、活动任务删除或按目录名猜测清理。

## 部署脚本已经退出，但后台程序仍持有输出

例如脚本执行 `service &` 后退出，后台程序可能继续继承作业的 stdout/stderr。此时直接关闭日志管道可能让后台程序收到 SIGPIPE，因此 helper 会继续读取，并单独保存直接脚本的退出证据。

状态包含数值 `script_exit_code`、`script_finished_at`，以及 `logs_draining=true`、`phase=waiting_for_output_close`。整体作业仍为 `running`，表示日志采集尚未结束；它不表示直接脚本还在执行，也不表示脚本或业务成功。**不要因为这个状态重新运行部署脚本。** 当前输出与后台进程情况应继续查询原 job ID。

准备阶段已明确要让应用独立运行时，启动命令应把 stdin/stdout/stderr 重定向到预定位置，例如：

```sh
your-service </dev/null >>/var/log/your-service.log 2>&1 &
```

这是后续部署脚本的配置示例，不是对已经启动的程序重放一次。真实程序路径、权限、启动方式和业务健康检查由项目定义；也可以交给已有服务管理器启动。输出流关闭后，helper 才发布整体作业的最终退出记录并允许日志清理。持有输出期间，清理预览标记为不可清理，工具不会为了“结束任务”强行关闭管道或杀死业务进程。
