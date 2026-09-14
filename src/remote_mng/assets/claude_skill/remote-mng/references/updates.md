# 安装更新与恢复

本页只处理 remote-mng 自身的本地更新。用户要求“更新 remote-mng”“检查新版”“更新失败了”时，从这里开始，不需要远端目标。所有命令仍通过 `<SKILL_BASE_DIR>/scripts/rmg.sh` 调用；示例占位符替换为实际值。

## 发现版本与发起更新

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update status --json
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update check --json
```

`status` 读取实际安装类型、程序/管理器/Skill 状态和更新历史，不把“没有独立安装目录”解释为“没有安装工具”。`check` 查询发布信息并固定目标版本、平台包与摘要，不安装、不重启管理器。私有仓库使用本机已有且有访问权限的 GitHub 认证；认证失败时报告修复建议，不索要令牌、不把凭据写进命令。

普通 uv tool 安装会验证环境和文件摘要，先保存原程序及全部依赖的离线回退 wheelhouse，再由独立候选环境替换。可编辑安装、未知 pip 环境或无法验证的已安装文件属于明确阻碍，不能直接重装覆盖。若 `--wait` 返回 `wait_skipped: true`，当前 CLI 必须退出以释放将被替换的 uv 环境；使用进度页，完成后按原 ID 查询，不另起同环境等待循环。

更新保留原 uv 依赖 constraints；依赖冲突时按失败与恢复信息处理，不自行移除约束。未知自定义 index/options/extras 或额外工具环境不自动覆盖。原 receipt/direct URL 属于私有恢复快照，不输出私有地址、不伪造 uv 当前安装记录；回退核对原程序、依赖及受管 Skill 字节。

只有用户要求检查时，报告当前版本、候选版本和阻碍即可。用户已经要求升级时，使用返回的 `plan_id` 继续，无需再为同一授权增加一次确认。先报告所选版本和实际影响，不能绕过活动会话、并发操作、Skill 本地修改或协议不兼容的阻碍。

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update --plan-id PLAN_ID --json
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update status --id UPDATE_ID --json
```

用户没有指定版本时，也可直接 `update --json`，由工具查找并固定可用正式版。指定版本用 `update check --tag vX.Y.Z --json`，不要自行拼下载地址、选择通配资产或忽略摘要。非默认仓库须来自用户明确选择或安装记录，不能从终端输出推断。

## 状态与中断

提交响应只表示已建立本地更新记录。保存 `id`，用同一个 ID 查询至终态；需要短暂等待可用 `update --plan-id PLAN_ID --wait --wait-timeout 30 --json`。等待超时和 Ctrl-C 只停止本地观察，不取消更新；不能因此提交第二次安装。连接丢失或响应丢失时，先查 `update status` 的历史。

- `succeeded`：核对返回的版本与验证结果，再报告升级成功。
- `blocked`：报告阻碍，保留活动会话和操作；不要自行关闭前台或停止业务任务以强行更新。
- `failed`、`interrupted`：读取该记录的错误和恢复信息；`failed` 且 `recovery.state=restored` 表示升级失败后已恢复旧版，不能称为升级成功。未确认恢复时按下面的 `recover` 入口处理。
- `rolled_back`：用户明确要求的兼容回退已完成，退出码为 `0`；报告实际回退版本，不把回退称为升级。

`update_worker_start_failed` 表示宿主不能启动独立更新进程。使用返回的正常终端或管理器入口建议；需要用户在普通系统终端发起时，说明宿主限制。不要循环启动、创建计划任务或借其他进程绕过宿主限制。

## 恢复失败或中断的更新

用户要求修复本次更新，或继续完成已授权更新的失败恢复时，使用历史中的原更新 ID：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update status --id FAILED_UPDATE_ID --json
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update recover --id FAILED_UPDATE_ID --wait --wait-timeout 30 --json
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update status --id RETURNED_UPDATE_ID --json
```

保存恢复响应返回的 `id`，持续查询该记录，不假定它与原失败 ID 相同。`recover` 使用已有缓存和旧版程序/旧 wheel 恢复该次更新之前的本地安装及管理器，不查新、不联网下载、不重提部署或测试任务。缺少缓存、缓存已变动、存在活动进程或恢复兼容性不明时，报告阻碍；不要补下载、删除更新 gate、手改指针或数据库绕过检查。

恢复完成后报告实际版本与验证结果，明确区分“已恢复原安装”和“已升级成功”。等待超时或响应丢失时仍按返回 ID 查询，不重复发起恢复。uv 当前环境返回 `wait_skipped` 时须让 CLI 退出，遵循上面的观察方式。

## 回退与组件边界

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update rollback --json
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update rollback --to-version X.Y.Z --json
```

回退同样保存更新 ID 并查询结果，只使用仍保留且验证兼容的版本。程序回退不等于数据库恢复；未知数据库或协议变化必须按工具报告处理，不能删除数据库或手改版本指针。

回到不含 `update` 命令的旧版（例如 0.3.0）后，通过保留的独立更新器或安装入口维护；不要假定旧 CLI 仍能执行更新命令，也不要删除恢复所需缓存。

更新会处理本地管理器和托管 Skill。新开 Claude Code 会话以加载新版 Skill。远端 helper 不会随着本地升级自动替换；只有任务需要且目标已有授权时，按 helper 的兼容检查及 [持久作业](jobs.md) 说明更新选定目标。普通交互终端不能重接；持久 job 保留原 ID，更新后按 ID 查询，不能重新执行部署或测试。

个人约定保存到 `skill status` 返回的 `user_extension.path`，更新不会改写它。托管 Skill 已有改动时，报告冲突并保留原文件；不要删除完整性清单或强制覆盖。
