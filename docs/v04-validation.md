# 0.4.0 更新机制验收记录

日期：2026-09-14。以下记录是发布前的本地 0.4.0 候选快照；最终 GitHub 制品以所选 Release 的摘要和对应 Actions 记录为准。

## 交付范围

- Agent 通过 `rmg update` 检查、提交、查询、回退和显式恢复；Skill 提供完整流程及结果判断。
- 网页显示安装方式、版本、更新来源、阻碍和历史；独立进度页不依赖正在替换的管理器。
- 更新前固定版本与摘要，准备并验证候选及回退材料，再检查活动会话与独立进程创建能力。
- 空闲管理器自动停止、恢复；保留任务与远端 job 引用，不重复提交远端工作。
- 支持受管独立包和能够验证归属的普通 uv tool 安装；uv 替换由独立运行环境接手，保留完整离线回退材料。
- 托管 Skill 随程序更新，个人约定放在独立的 `USER.md`。不自动更新远端 helper，不执行隐式数据库迁移。

日常使用及限制见 [更新说明](updates.md)。

## 自动化回归

| 环境 | 结果 |
| --- | --- |
| Windows / Python 3.12.11 | 525 passed，60 skipped，62.15 秒 |
| 发布前 Windows 全量复验（含新增 8 项） | 533 passed，60 skipped，64.06 秒 |
| WSL Ubuntu / 独立测试 Python 环境 | 570 passed，15 skipped，68.68 秒 |
| 后增受限模式验收判定测试 | Windows、WSL 各 8 passed |
| Ruff | 通过 |

跳过项来自平台和可用外部工具条件，不计为通过。回归覆盖活动操作竞争、持久计划幂等、更新进程丢失、维护门禁、离线恢复、uv 环境归属及文件完整性、网页边界和实际前端 JavaScript 行为。新增 Windows 回归要求独立进程预检失败时不停止旧管理器、不切换版本、不留下维护门禁。

## 发行包与真实进程验收

最终候选使用主机原生 PyInstaller 构建 ZIP，并输出通用 Python wheel、SHA256 和构建记录。所有验收使用隔离目录，未更新用户全局安装或真实远端设备。

| 平台 | 最终 ZIP SHA256 |
| --- | --- |
| Windows x86_64 | `890da9394b162f86b3e87aa5b508d079520958494ae715a1b8919da2754c3254` |
| Linux x86_64 | `6ab9bb48de3f6c5ca828991b653fd9e4d7b60fd9427e7032e6876f957c1ffa8c` |

本地证据目录分别为 `.test-state/bundle-windows-v040-r2` 和 WSL 的 `/home/lixiao/.cache/remote-mng-bundle-linux-v040-r2`。目录包含 `build-result.json`、`smoke-result.json`、`worker-smoke-result.json`、`source-worker-smoke-result.json`，部分失败诊断另保留在专属测试目录。制品摘要绑定具体快照，同版本后续修改不能覆盖正式发布资产。

三个验收层次：

1. `scripts/smoke_release.py` 使用真实保留的 0.3.0 ZIP，验证升级及回退、稳定入口、Skill、数据库，以及本地 SSH/SFTP/Telnet 夹具。Linux 额外验证持久 helper job 的副作用次数恰好为一次。
2. `scripts/smoke_update.py` 使用真实 CLI 和独立更新进程，验证管理器停启期间进度页仍可读、任务 ID 保留，以及显式回退。GitHub 下载仅替换为固定本地制品运输夹具，不替换安装器和管理器。
3. `scripts/smoke_source_update.py` 验证真实 uv 环境替换、原 CLI 退出、source 到 frozen 更新进程交接、进度页端点切换及离线回退。前驱是当前源码构建的 `0.3.99` 测试夹具，不能当作官方 0.3.0 的自动更新验收。

本地 Windows 前驱与已发布的 0.3.0 资产一致（SHA256 `dc72ddcca4030ab745c5db44e17f6e15d5f49b0e47d8e7415852884d755626d1`）。本地 Linux 前驱是保留的 0.3.0 构建（`b7716a21c8d93608998af6d35a9686ef5d4924a8230c3c30400c2ad055c885c5`），不是 GitHub 上的正式 Linux ZIP（`47024455887cc949df3e58001c4078a852b7302a8a3997fe818a766b5f00c175`）。发布流水线从已认证的 GitHub 下载正式资产，单独验证这条正式基线路径。

Linux r2 的上述三层默认验收全部通过，uv 全链路耗时 77.09 秒。Windows r2 的分发层通过；独立 worker 在本机受限模式下通过“提前阻碍并保留原状态”的验收，耗时 35.74 秒，未完成自动更新和回退。原管理器 PID 在阻碍后仍在线，无停机和切换事件，无维护门禁残留。

Windows r2 的真实 uv 受限模式也通过，耗时 22.66 秒，结果为 `passed_blocked_safely`。source 到 frozen 的实际进程及进度端点交接完成，随后预检返回 `blocked`；原 uv 非字节码文件、托管 Skill、管理器 PID、任务、job 引用和 `USER.md` 均保持不变，没有重装标记、数据备份或维护门禁。这验证保护机制，不表示已升级 uv 安装。

## Windows 支持边界

本机 Codex 宿主存在嵌套 Job Object 限制：第一层允许 BREAKAWAY，进入的上一层限制不允许更新进程再次独立派生管理器。只读诊断观察到相应限制标志 `0x2800` 和 `0x2000`，生产创建请求实际返回 WinError 5。微软对 [嵌套作业的 breakaway 规则](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs) 有对应说明；仅凭 `in_job=true` 本身不能得出此结论。

此实现保留正式独立启动要求，新增切换前的真实创建预检。受限宿主应得到 `blocked`，原版本、Skill 和运行中的管理器保留；这属于阻碍处理验收，不能写成更新成功。普通子进程健康探测只验证候选功能，也不能替代生产独立启动验收。

发布流水线默认必须通过完整更新和回退，未将本机的受限模式作为 Windows 发布通过条件。尚需在允许该进程创建方式的 Windows 环境运行完整发布验收，不能据此宣称所有 Windows Agent 宿主均可自动更新。

## 迁移与未覆盖范围

- 官方 0.3.0 尚无统一 `update` 入口，需要一次旧版安装流程进入 0.4.0；之后才使用新流程。
- 编辑安装、未知 pip 环境、无法验证的文件或自定义 uv 环境会报告阻碍，保留原环境。
- 未在用户的真实 CTP 设备或生产服务器执行更新；本地夹具通过不等于设备兼容验收。
- Skill 文件同步后仍需新开 Claude Code 会话，不声称当前会话热加载。
- 只读独立创建预检反映当时能力，不能保证后续宿主权限永不变化；运行中失败仍保留记录和恢复路径。
