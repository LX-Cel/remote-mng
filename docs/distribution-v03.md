# 0.3 独立发行包验证记录

日期：2026-09-13。对应实现与操作指南见 [distribution.md](distribution.md)。本节记录隔离的本地构建与受控 loopback 测试；这些试验使用独立目录。个人已有安装的升级和 GitHub 发行状态另见 [交付总表](v03-validation.md)，不与隔离试验混为一项。

## Agent 跟进试验使用的固定快照

| 平台 | 本地构建环境 | ZIP 字节数 | SHA256 |
| --- | --- | ---: | --- |
| Windows x86_64 | Windows 11，Python 3.12.11，PyInstaller 6.22.3 | 29,951,309 | `ea39ea93ace1291ca1e8b5dc7b3090c61b6e773ea8bcb0e888048f3d88aa717b` |
| Linux x86_64 | WSL2 Ubuntu 24.04，Python 3.12.3，glibc 2.39，PyInstaller 6.22.3 | 31,587,871 | `b7716a21c8d93608998af6d35a9686ef5d4924a8230c3c30400c2ad055c885c5` |

构建输出使用版本 `0.3.0`。这组 `ready` 制品保留不变，Windows 的摘要对应最终两次真实 Claude 跟进试验；不是后续最终发布源码构建的摘要。不保证以后从同版本源码重新构建字节完全相同；使用时以该次 release 随附的校验文件为准。依赖为 AsyncSSH 2.24.0、cryptography 50.0.1、telnetlib3 4.0.5。

本地制品位置（忽略目录，不进入 Git）：

- Windows：`.test-state/bundle-windows-v03-ready/remote-mng-0.3.0-windows-x86_64.zip`；同目录 `build-result.json`、`smoke-result.json`。
- WSL：`/home/lixiao/.cache/remote-mng-bundle-linux-v03-ready/remote-mng-0.3.0-linux-x86_64.zip`；同目录保存构建与 smoke JSON。

Linux ZIP 明确记录 glibc 2.39。release workflow 另外选择 Ubuntu 22.04/glibc 2.35 构建，必须由该工作流自身执行并通过 smoke 后，才能声称该基线已验收；本记录没有把 WSL 的通过结果替代 CI 结果。

## 补充修复后的本地发行验证包

下列本地包完成后，发布源码另收紧了 `job_ready` 预检：旧或未知存储协议、缺少轮转能力、存储健康不可确认时均不会宣称可提交新作业。这一诊断修复由专项回归覆盖，并进入 GitHub 发行流水线的全量测试与实际包 smoke；下列摘要仍对应修复之前的本地包，不冒充随后发布的 CI 资产。

在上述 Agent 试验快照之后，最终源码增加两项变更：cleanup 预览计划绑定目标配置指纹，阻止两台设备的同名作业共用删除确认；Skill 校正只读操作参数示例，并要求总结已恢复的中途错误。未更改版本、安装协议、数据库结构或 helper 资源。cleanup 指纹有三个 Windows/WSL 专项回归；未为这两项改动重新运行模型试验或跨版本迁移实验。

修复源码另建以下两个本地制品，保留 `ready` 目录及其试验摘要不变：

| 平台 | ZIP 字节数 | SHA256 |
| --- | ---: | --- |
| Windows x86_64 | 29,950,710 | `c3411b31e84a38458483b9420d7bab26937bbcff7c0ac1fbe9236bd5e20424e6` |
| Linux x86_64 / glibc 2.39 | 31,587,425 | `d4b54c2da5f181c819229ead4b5e1fa46b1d19d3f96e3314484cbfb6e64fe025` |

- Windows：`.test-state/bundle-windows-v03-release/remote-mng-0.3.0-windows-x86_64.zip`。
- WSL：`/home/lixiao/.cache/remote-mng-bundle-linux-v03-release/remote-mng-0.3.0-linux-x86_64.zip`。

这两个摘要记录最终源码在本机的构建产物。GitHub tag 工作流会在自己的 Windows/Ubuntu 22.04 环境重新构建，实际发布资产的摘要以 release 校验文件为准，不能用本表替代。

两个最终本地制品均重新通过下节全部适用的实际包 smoke，结果分别保存在同目录 `smoke-result.json`；Linux 再次验证 detached helper 副作用计数恰好一次。测试创建的隔离 daemon 均已正常停止，没有修改全局安装或个人 Skill。

## 从实际发行包运行的验证

两平台都在仓库之外的临时目录安装，路径包含中文、空格和单引号；CLI 与后台 daemon 都从 onedir 中的 frozen 可执行文件启动。测试进程 PATH 仅保留系统工具，移除开发 Python，且不传递 PYTHONPATH。

两平台共同通过：

1. 从校验过的 ZIP 安装到版本目录；检查逐文件 SHA256、平台和协议；健康通过后切换稳定入口。
2. 自动安装 Claude Skill，验证资源完整，并实际通过其 `scripts/rmg.sh` 调用 frozen `runtime-info`。Windows 使用 Git Bash → 包装脚本 → 稳定 shell 入口 → 版本目录中的 rmg.exe，实际验证嵌入双引号/反斜杠的主机参数、`--file -` 标准输入 JSON、包含 `printf` 的原样命令。独立 argv 回归还覆盖空参数、中文、尾部反斜杠与 shell 元字符。
3. 检查网页、主 helper、存储 helper、Skill 资源均存在；实际进行 Ed25519 签名/验签，避免只检验模块 import。
4. 提交 CLI 已退出后，独立 daemon 仍可接收下一次调用。通过 loopback SSH 验证 stdout、stderr 和退出码 7，UTF-8 中文输出正确。
5. SFTP 上传测试包并校验远端文件的实际字节；Telnet 输入、结束匹配、同一 request ID 重用均正确。
6. 停止 daemon 后重装同一已验证制品，再经过兼容回退流程选择已安装版本；原 SQLite 任务 ID 仍可查询。

Linux 额外通过真实 detached helper 测试：作业写入计数文件，CLI 返回后停止本地 daemon，完成程序重装/回退后重新查询原 job ID 和日志。作业成功完成，计数文件恰好一个 `X`，没有重新提交业务输入。

首次安装脚本也分别执行过：Windows PowerShell 5.1 `install-release.ps1` 和 Linux `install-release.sh` 均完成安装、Skill、doctor 并默认启动 daemon，查询实际运行版本为 0.3.0；测试结束后正常停止这些隔离服务。Windows 验证使用 SHA `fb4a5fd7…` 的相同程序快照（最终快照仅进一步精简 Skill 文案），Linux 使用上表最终包。WSL 起初没有 Info-ZIP unzip，脚本正确停止；测试从 Ubuntu noble-updates 下载 `unzip 6.0-28ubuntu4.1`，仅解包到测试缓存并显式加入该次 PATH，未修改系统软件安装。

`setup --start-daemon` 支持首次安装时在普通终端外部预启动。Windows Agent 宿主拒绝 `CREATE_BREAKAWAY_FROM_JOB` 时，工具不使用 WMI、计划任务或其他绕过；测试覆盖启动失败后保留已安装 Skill、报告 `setup_daemon_failed`，并且仅尝试一次启动。生成的一键入口位于安装目录 `bin/Open remote-mng.cmd` 和 `bin/open-console.sh`，电脑重启后可用来启动并打开本地状态页。

## 故障回归

`tests/test_distribution.py` 与既有 `tests/test_skill_install.py` 合计：Windows **51 passed / 7 skipped**；WSL **57 passed / 1 skipped**。Windows 跳过的是既有符号链接/虚拟环境链接相关环境限制；WSL 跳过 Windows argv 专项。

覆盖：摘要错误时不执行候选程序、路径穿越及多余文件拒绝、错误平台与未知协议拒绝、活动 daemon 不强停、修改过的 Skill 不覆盖、候选健康失败不切换、激活失败精确恢复原 Skill、兼容多版本升级/回退保留数据库，以及 source/frozen 启动参数和绑定差异。

实际打包 smoke 与完整调用链回归发现并修复：

- Windows 中 `server.stop` 返回且删除运行标记后，进程仍可能短暂持有日志/DLL。验证器现在等待该已知 PID 完全退出后清理测试目录，不发送终止信号。
- Windows AsyncSSH 运行路径间接依赖动态导入的 `win32timezone`。将其明确加入 frozen hidden imports 后，实际 SSH 请求通过。
- Windows PowerShell 5.1 普通 native 参数展开会丢失嵌入双引号；管理入口改用 CRT 引号序列化。其 `-File` 主机解析器还会在脚本之前拒绝独立 `-`，所以 Agent Skill 直接使用稳定 shell 入口并保留 stdin。版本指针统一使用 LF，同时兼容旧 CRLF 指针。

另外在 Windows 完成了真实 frozen 制品跨版本切换：`0.3.0 → 0.3.1-smoke.1 → 0.3.0`。第二个制品来自仓库外的源码副本，仅更改版本标记用于验收，未修改主项目版本或发布该测试版本。每次切换都运行候选 daemon/Skill 健康检查，启动后返回正确的新/旧版本；稳定 Skill 绑定不变，同一 SQLite 任务 ID 在升级和回退后均可查询。制品摘要分别为 `fb4a5fd729c71586918de65070db481cb28d1fb8d37000ea799a8d8ddc155eab` 和 `63fd7c57de2fe93e1a72551d6d640a3217e0853e3aab2bbb0f678c10f9f747ce`，记录在 `.test-state/upgrade-bootstrap-result.json`。该测试验证兼容协议与数据库下的真实程序切换，不代表未来任意版本或跨数据库迁移都可回退；失败恢复边界仍由确定性测试覆盖。

没有真实 CTP 设备，未把 TI 业务验收、任意企业网络、ARM 或 glibc 2.35 兼容性写成已经验证。真实 Claude 的固定试验与最后两次新包跟进，单独记录在 [validation-agent-v03.md](validation-agent-v03.md)，不把 loopback smoke 冒充 Agent 自主成功率。
