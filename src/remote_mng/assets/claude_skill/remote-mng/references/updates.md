# 安装更新与恢复

本页只处理 remote-mng 自身的本地更新。用户要求“更新 remote-mng”“检查新版”“更新失败了”时，从这里开始，不需要远端目标。所有命令仍通过 `<SKILL_BASE_DIR>/scripts/rmg.sh` 调用；示例占位符替换为实际值。

## 先区分离线与在线安装

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update status --json
```

`status` 读取 `delivery`、实际安装类型、程序/管理器/Skill 状态和更新历史，不把“没有独立安装目录”解释为“没有安装工具”。

当 `delivery: offline` 时，当前安装使用离线包维护。告诉用户当前实际版本，并说明需要将适合本机系统的新版离线安装包带入当前环境运行；不要联网检查 GitHub，不要主动改成在线或复用历史联网计划。`update check` 或普通 `update` 返回 `blocked` 并包含 `offline_update_required` 阻碍，是离线指引，不代表已查到新版、已是最新版或更新成功。

用户提供了获准的本地安装包路径并要求更新时，核对来源和本机平台，先检查本地活动，再运行该文件。Windows 使用 `...-setup.exe`，Linux/WSL 使用已有执行权限的 `...-setup.run`；安装包自带程序、运行依赖、Skill 和 helper，不要补下载资源或手工传入 ZIP/SHA。需要结构化结果可加 `--json`。保留原更新 ID 并核对终态，完成后重新检查程序、管理器和 Skill；用户需新开 Claude Code 会话加载新 Skill。重新运行旧包不会获取新版。

离线包只支持新装和受管独立安装的安全更新，暂不支持旧 uv/Python 环境的离线迁移。遇到相应阻碍时保留原环境和绑定，报告原因，不调用 uv 联网、不卸载重装，也不删除 Skill 或安装清单来伪装成新装。0.6 离线安装器尚未发布时，应说明候选状态，不声称已有下载。

## 在线安装：发现版本与发起更新

只有实际安装为在线方式，或用户明确选择在线安装时，才使用下面流程：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update check --json
```

`check` 查询发布信息并固定目标版本、平台包与摘要，不安装、不重启管理器。公开仓库通过 HTTPS 访问，无需安装或登录 GitHub CLI；已认证的 `gh` 只作为私有发行源的后备，不要为公开仓库新增认证步骤。认证失败时报告建议，不索要令牌、不把凭据写进命令。

未指定通道时跟随安装时保存的通道。`stable` 只选择正式发布；`preview` 选择最高版本的正式或预发布。用户通过预览入口安装后，不要擅自改回 stable 造成找不到更新。用户要求“以后只用正式版”或“跟随预览”时，分别使用 `update check --channel stable --json` 或 `update check --channel preview --json`，实际安装成功后保存选择；`--preview` 是 preview 快捷参数。选择正式通道不授权把较新的预览自动降回更旧版本。

普通 uv tool 安装会验证环境和文件摘要，先保存原程序及全部依赖的离线回退 wheelhouse，再由独立候选环境替换。可编辑安装、未知 pip 环境或无法验证的已安装文件属于明确阻碍，不能直接重装覆盖。若 `--wait` 返回 `wait_skipped: true`，当前 CLI 必须退出以释放将被替换的 uv 环境；使用进度页，完成后按原 ID 查询，不另起同环境等待循环。

更新保留原 uv 依赖 constraints；依赖冲突时按失败与恢复信息处理，不自行移除约束。未知自定义 index/options/extras 或额外工具环境不自动覆盖。原 receipt/direct URL 属于私有恢复快照，不输出私有地址、不伪造 uv 当前安装记录；回退核对原程序、依赖及受管 Skill 字节。

只有用户要求检查时，报告当前版本、候选版本和阻碍即可。用户已经要求升级时，使用返回的 `plan_id` 继续，无需再为同一授权增加一次确认。先报告所选版本和实际影响，不能绕过活动会话、并发操作、Skill 本地修改或协议不兼容的阻碍。

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update --plan-id PLAN_ID --json
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" update status --id UPDATE_ID --json
```

用户没有指定版本时，也可直接 `update --json`，由工具按保存的通道查找并固定可用版本。指定版本用 `update check --tag vX.Y.Z --json`，不要自行拼下载地址、选择通配资产或忽略摘要。非默认仓库须来自用户明确选择或安装记录，不能从终端输出推断。

## 用户要求手动更新或旧版引导

用户希望手动更新时，默认指导其运行新版离线安装包，保留离线方式。只有用户选择在线更新时，才指导其在普通系统终端重新运行原来的 `install.ps1` / `install.sh`；同一入口识别安装和更新，开发者不需要选择 ZIP、复制 SHA、停服务、重装和另装 Skill。在线脚本来自项目根目录，先下载到本地供查看再运行，不使用 `curl | sh` 或 `iex` 执行远程内容。预览期脚本会明确提示并选择兼容预览，随后保存通道。只有支持 bootstrap 的 0.5 及之后发布制品可使用该在线入口；尚未发布的功能要说明实际状态，不声称已有可下载版本。

旧 0.3 管理器缺少安全维护预留协议，运行中会成为明确阻碍。先检查活动和原 job ID，在用户确认普通前台可以断开后处理；不要强停旧管理器或业务任务来消除阻碍。候选 bootstrap 对受支持的普通 uv 安装先保存原环境和依赖快照，再安全更新；可编辑、未知 pip、归属或文件验证失败的环境必须保留并报告，不用无保护的 reinstall 作为默认替代。

## 状态与中断

提交响应只表示已建立本地更新记录。保存 `id`，用同一个 ID 查询至终态；需要短暂等待可用 `update --plan-id PLAN_ID --wait --wait-timeout 30 --json`。等待超时和 Ctrl-C 只停止本地观察，不取消更新；不能因此提交第二次安装。连接丢失或响应丢失时，先查 `update status` 的历史。

- `succeeded`：核对返回的版本与验证结果，再报告升级成功。
- `blocked`：报告阻碍，保留活动会话和操作；不要自行关闭前台或停止业务任务以强行更新。若包含 `offline_update_required`，需要新版离线安装包，本机没有联网查新或开始更新；不能不断重试查询或称为“已是最新”。
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
