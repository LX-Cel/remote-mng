# 0.6.0 离线安装器验收

日期：2026-10-21。当前记录本地候选，实际发行与公开下载验证将在完成后补充。

## 验证范围

- Windows 全量：847 passed，109 skipped，120.09 秒。
- WSL Ubuntu 全量：868 passed，54 skipped，99.11 秒。
- 安装器纯标准库及打包校验定向测试：Windows、WSL 各 63 passed。
- 离线策略和原更新行为定向回归：Windows、WSL 各 114 passed。
- 网页及前端：76 passed；Skill 安装：31 passed、8 skipped；Ruff、diff check 通过。

跳过项不计为通过。平台测试与本地夹具不能替代用户真实设备的兼容验证。

## 真实单文件安装

Windows 使用真正的 PyInstaller onefile EXE，Linux/WSL 使用原生 ELF `.run`；两者内含原始发行 ZIP，未重新压制 ZIP，也不依赖系统解压程序。日常程序仍安装到稳定目录，不从安装器的临时展开目录运行长期管理器。

`scripts/smoke_bootstrap.py --offline-installer-result ...` 运行实际安装文件，使用隔离 HOME、程序、状态和 Claude 配置目录。`RMG_RELEASE_TRANSPORT` 被设为拒绝值，外部 HTTP 代理指向关闭的回环端口，loopback 管理器通信保留。验证：

- 首次安装及完整校验、Skill 和管理器可用。
- 无目录参数重复运行复用原安装位置、状态和管理器 PID。
- 官方 0.5 运行中的空闲管理器升级到 0.6，保留任务、USER.md、Skill 和实际安装路径。
- 保存 `delivery: offline`，`update check` 在任何发布查询前返回离线安装包指引，不宣称已检查最新版。

Windows 全链路 58.61 秒通过；WSL 全链路 26.35 秒通过。两平台测试管理器均已正常停止，未更新用户的全局安装或实际远端设备。

| 本地候选 | SHA256 |
| --- | --- |
| Windows 原始 ZIP | `877065fb2ac51e652404df1fd7929846500244e9a8d02b1412afdeb4d272891b` |
| Windows setup.exe（38,120,835 字节） | `506ef9e41b73bc7a11e139db854d5786f279456c0db95c3ea9b98693deff2fc5` |
| WSL 原始 ZIP | `51fef1c749c0c9f9bd5d6f006d8b99fb2928b4444dc5d8c46836e680ec5d7657` |
| WSL setup.run（39,037,776 字节） | `67247589985140577589b57e6b49e66133be81d09cdcd0566334ff896819603a` |

本机 WSL 包基于 glibc 2.39，不能当作 Ubuntu 22.04 / glibc 2.35 发行包。Windows 构建记录逐项核对源码和 wheel，构建期间输入未变化。证据目录为 `.test-state/bundle-v060-windows` 和 WSL `/home/lixiao/.cache/remote-mng-v060`。

## 明确边界

旧 uv/Python 安装的离线迁移在发布查询、快照或停止旧管理器前被拒绝。独立包更新缺少离线缓存时不调用下载器；活动检查、失败回退和显式恢复沿用原机制。离线偏好拦截默认查新及旧在线计划，但不会禁用用户授权的远端 SSH/Telnet 业务连接。

内含 SHA 和清单检查保证文件一致性，不能独立认证首次下载来源。安装包需从可信项目发布或公司内部制品源取得。Windows 的宿主进程限制仍适用，安装成功不能推断全部 Agent 宿主的后台更新都能工作；正式发行 CI 不以 `passed_blocked_safely` 代替完整成功。
