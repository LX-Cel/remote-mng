# 0.5.0 简易安装与更新验收

日期：2026-10-21。本文区分本地候选验收与实际发布结果。

## 实际发布：v0.5.0 预发布

[v0.5.0 Release](https://github.com/LX-Cel/remote-mng/releases/tag/v0.5.0) 基于提交 `8ab011b517bfc589cd7ed0b083352ad475ef1fe5`，仓库公开，保留预发布标记且不替代正式 Latest。安装脚本默认会说明预览状态，再选择支持新入口的 0.5；旧版资产未修改。

- [Windows/Linux × Python 3.11–3.13 六组基础 CI](https://github.com/LX-Cel/remote-mng/actions/runs/39552197928) 全部通过。
- [发行验收](https://github.com/LX-Cel/remote-mng/actions/runs/39552210533)：Ubuntu 22.04 全部通过，包括官方 0.4 分发升级/回退、首装/重跑/升级、独立 worker 更新/回退和 uv 交接/离线恢复。uv 后台前驱为当前源码的 0.3.99 夹具，不冒充历史正式版。
- Windows CI 的 Git Bash 测试路径定位及宿主独立进程限制导致发行检查失败，正式自动发布未执行。预发布使用同源码的本机 r2 Windows 包，未使用失败的 Windows CI 包。r2 根 PowerShell 安装器的首装、重跑、官方 0.4 升级通过；通用 bootstrap 流程 49.51 秒通过；分发升级/回退、Git Bash/Skill、SSH/SFTP/Telnet、任务保留均通过。
- 最终 r2 官方 0.4 wheel 到 0.5 的 uv 前台升级及同版本重复运行：Windows 29.23 秒、WSL 12.07 秒，均通过。返回目录确为原 uv prefix，重复运行保留管理器 PID、receipt 和 Skill。恢复失败不再无条件宣称旧版已恢复。
- WSL r2 根脚本真实完整流程 23.68 秒、通用 bootstrap 官方 0.4 升级 20.56 秒通过；安装器和 POSIX 最终定向回归共 79 项通过。本地 WSL r2 为 glibc 2.39 构建，未作为公开 Linux 包发行。

| 实际发布资产 | SHA256 | 来源 |
| --- | --- | --- |
| Windows ZIP | `b865d0c48b7c02df84d351c4b1ec5dc2aeeeeb7dd2e5433730f41d256fc6c60e` | 本机 r2，构建 wheel 源码与提交源码逐文件一致 |
| Linux ZIP | `2770eb3a1f1d9771de363189f384dce81d9b57f50cc2653a9c30ba449da5cdf4` | 通过完整验收的 Ubuntu 22.04 CI，glibc 2.35 |
| 通用 wheel | `6664c13da4c6f5c82104f917baa0dd60137d2fb6bdbc3532187183078cd5a59f` | 同次 Linux CI |

Release 附带安装脚本、各资产摘要、`SHA256SUMS.txt` 和 `release-validation.json`，记录来源与范围。前台安装成功不能替代所有 Windows 宿主的后台更新验收。

首次匿名公网检查发现 PowerShell 对 REST 数组的枚举差异；仅修正根 `install.ps1` 的响应展开，PS5/PS7 脚本回归 69 项通过。脚本和摘要作为安装入口修订 2 同步，ZIP/wheel 未替换。本机 WSL 公网检查在下载脚本前被 DNS 超时阻断，未执行安装，不计为安装通过或产品失败；上方 Linux CI 的真实制品验收独立成立。

## 已完成的本地验证

- 首轮本地全量：Windows 756 passed，105 skipped，113.92 秒；WSL 779 passed，50 skipped，96.06 秒；Ruff 通过。WSL 首轮发现真实 Bash 测试读入用户启动配置而触及 0.5 秒截止线，修正测试启动配置隔离后通过；生产 Shell 实现与严格超时断言未修改。最后的状态显示修复另做定向回归，并由上方最终提交的六组 CI 复验。
- 安装器回归：Windows、WSL 各 30 passed；覆盖真实文件安装、上下文复用、状态保留、并发阻碍、原版本恢复和终态退出码。外部程序边界由夹具代替，不能代替下面的真实制品验收。
- Windows 根 `install.ps1`：真实 0.5 包首次安装、无参数重复运行及官方 0.4 ZIP 到 0.5 的升级全部通过。仅 HTTPS 发布传输使用固定本地资产，候选程序、Skill、独立管理器、doctor 和网页均真实运行。任务、USER.md、目录及重复运行前后的管理器 PID 按各阶段核对，测试后管理器已停止。
- WSL 根 `install.sh`：同样三阶段通过，32.91 秒。前驱为保留的真实 0.4-r2 测试包，不能当作 GitHub 官方 0.4 Linux 资产。
- 新增跨平台 `smoke_bootstrap.py`：Windows 使用官方 0.4 ZIP，53.39 秒；WSL 使用保留的 0.4-r2 ZIP，21.46 秒，均通过。该脚本加入发行流水线，默认要求实际安装成功，安全阻碍不算成功。
- 补验官方 0.4 Linux ZIP（SHA256 `0e66c42ec4b6e428803775ed31ca31454160153c61758ab29ca161fce7fdf28d`）到本机 0.5：22.30 秒，通过首装、重跑和跨版本升级。目标包仍为下表的本机 glibc 2.39 构建。
- 发布查询和下载：公共 HTTPS 访问已验证无需 gh 登录；单元回归覆盖摘要、资产名、重定向、下载上限、通道与错误分流。
- 官方 0.4 通用 wheel（SHA256 `6b30f199beafc8e96b44c807d66cf425dbc25f1477a3315cdb8cdc0489ddb033`）的真实 uv 安装到 0.5 前台升级：Windows 22.67 秒、WSL 9.29 秒，均通过。原 uv 目录、任务、远端 job 引用、USER.md 和经校验的 Skill 绑定保留；持久恢复更新器存在，没有改成独立安装。旧环境使用完整真实依赖，发布传输替换为固定资产，替换流程和管理器均真实运行。

所有现场使用隔离的程序、状态和 Claude 配置目录，未更新用户全局安装，也未操作真实远端设备。

## 首轮本地制品来源（不是实际发布资产）

| 制品 | SHA256 | 来源 |
| --- | --- | --- |
| Windows 0.5 ZIP | `4e45e42ad0554ac6a8d64c5eaa4acb7287637638fee6b590b46fad22dbc68514` | 本机原生构建，wheel 内源码与当前源码逐文件一致 |
| WSL 0.5 ZIP | `5be29f0bcc265435274798d8ae3eab3ff3a3ce09eb6c15b6b123a85b81aa32ca` | Ubuntu 24.04，glibc 2.39；不是 Ubuntu 22.04 发行包 |
| Windows 验收用官方 0.4 ZIP | `754c7ed9da4df771b8ddb418515ca025d203ac10708b144c000dc9518f751c9f` | GitHub v0.4.0 Windows 资产 |
| WSL 验收用保留 0.4-r2 ZIP | `6ab9bb48de3f6c5ca828991b653fd9e4d7b60fd9427e7032e6876f957c1ffa8c` | 历史本地候选，不冒充正式发布 |

本地证据位于 `.test-state/easy-v050-windows` 和 WSL `/home/lixiao/.cache/remote-mng-easy-v050`，包含构建结果、来源摘要和实际安装日志；这些测试目录不纳入仓库。

## 执行与平台边界

手动安装脚本在前台执行候选安装流程，沿用更新日志、活动检查、维护门禁和恢复材料。它不需要额外派生后台更新 worker，因此与 Agent 默认的独立后台更新具有不同进程层级；前台流程通过不能证明后台流程在所有 Windows 宿主中均可创建管理器。

后台 Agent 更新保留原生产独立进程参数及切换前预检。嵌套 Job Object 不允许所需进程创建时，预期结果是提前 `blocked`、保留旧管理器和安装，而非升级成功。没有通过 WMI、计划任务、其他宿主或放宽生产参数绕过限制。正式发行流水线仍要求完整更新和回退成功，未将 `passed_blocked_safely` 当作通过。

0.4 的历史验收不能替代本版结果，真实嵌入式设备兼容性仍需用户试用。
