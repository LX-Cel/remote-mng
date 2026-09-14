# 个人安装、独立发行包和升级

remote-mng 0.4.0 由 Python 实现。独立发行包把解释器、依赖、网页、远端 helper 和 Claude Skill 一起放在一个目录中，使用者无需安装 Python。Agent 通过已安装的 Skill 使用稳定入口；升级只切换入口指向的版本，不需要重新记命令。

源码/uv 安装继续支持。已在持久环境安装 `rmg` 后，在普通本地终端执行 `rmg setup --start-daemon --json`，一次完成 Skill 安装、本地 daemon 启动和 doctor。它不会连接远端或修改 Claude 的其他设置；已有 Skill 被修改时明确拒绝覆盖。去掉 `--start-daemon` 则只安装 Skill 与检查本地状态。`rmg runtime-info --json` 可以检查当前运行形式、版本、资源与密码学后端。

部分 Windows Agent 宿主限制独立后台进程创建。首次安装可在 Claude Code 外的普通终端运行，安装脚本默认尝试启动本地服务并报告实际结果。电脑重启后，Windows 用户可双击安装目录 `bin/Open remote-mng.cmd`，Linux/WSL 用户可运行 `bin/open-console.sh`，尝试启动并打开状态页，无需记忆业务 CLI。普通终端和一键入口也要以实际启动结果为准；预启动管理器不能证明后续更新进程也能独立创建新管理器。工具不会绕过宿主限制、创建计划任务或系统服务。

## 独立包的范围

- 0.4.0 的客户端构建范围是 Windows x86_64 和 Linux x86_64；WSL 使用 Linux 包。未声明 macOS、ARM 或任意嵌入式 Linux 的本地客户端兼容性。自动更新还需要通过当前宿主中的独立进程创建预检，具体已验证范围见 [0.4 验收记录](v04-validation.md)。
- Windows 发行包使用 Windows 10 及之后版本的系统接口。远端 Linux helper 不受客户端 Windows 架构影响。
- Linux 包依赖宿主 glibc。`rmg-release.json` 中的 `runtime_requirements.glibc` 记录构建系统版本；安装器拒绝低于该版本的系统。CI 在 Ubuntu 22.04 构建，使用 glibc 2.35 基线。本地 WSL Ubuntu 24.04 构建的验证包记录 glibc 2.39，不能直接把其测试结果当作 glibc 2.35 已验证。
- 采用 PyInstaller onedir，不是单个 Go 风格的可执行文件。必须保留整个版本目录；删除 `_internal` 会破坏运行。

## 首次安装

从有访问权限的 [GitHub Releases](https://github.com/LX-Cel/remote-mng/releases) 选择已实际发布的固定版本，下载对应 ZIP 和 `.sha256`。仓库目前为 **PRIVATE**，使用自己有仓库权限的 GitHub 身份；不要把 GitHub token 写入 remote-mng 配置。先核对 release 身份与完整 SHA256，再使用仓库内可检查的安装脚本。

以下安装示例面向 0.4.0。发布工作流会附带 `install-release.ps1`、`install-release.sh` 及其校验文件，以实际发布页资产为准；未发布的 tag 或本地构建记录不代表已经可下载。只下载发布资产时，可将脚本与 ZIP 放在同一目录，把下方的 `scripts/` 前缀去掉；不必为安装获取开发环境。阅读脚本后执行一次，之后由 Agent 使用 Skill。

Windows PowerShell：

```powershell
.\scripts\install-release.ps1 -Archive '.\remote-mng-0.4.0-windows-x86_64.zip' -Sha256 '<64位摘要>'
```

Linux/WSL（需要系统已有 `unzip` 和 `sha256sum`）：

```sh
sh scripts/install-release.sh ./remote-mng-0.4.0-linux-x86_64.zip '<64位摘要>'
```

脚本先校验 ZIP，然后运行其安装器；安装器再验证清单中的全部文件。安装成功后默认尝试启动本地 daemon，并报告实际结果；需要仅安装时，Windows 添加 `-NoStart`，Linux 在摘要后添加 `--no-start`。也可以从已有 source 或 frozen `rmg` 执行以下仅安装命令，再按需要运行安装目录 `bin/start-manager.ps1` 或 `bin/start-manager.sh`：

```sh
rmg distribution install ./remote-mng-0.4.0-linux-x86_64.zip --sha256 '<64位摘要>' --json
```

默认安装到 Windows `%LOCALAPPDATA%/remote-mng` 或 Linux `~/.local/share/remote-mng`，也可显式提供 `--install-dir`。`--home` 选择状态目录，`--claude-dir` 选择 Claude 配置目录；后续升级默认保留同一安装记录里的选择。不会修改全局 PATH，也不会更改其他用户环境。首次安装会创建并绑定 Claude Skill，重启 Claude Code 后即可用自然语言驱动。

目录结构：

```text
remote-mng/
  current-version                   # 原子替换的版本指针
  .remote-mng-install.json           # 管理归属、协议、前一版本与目录选择
  bin/rmg                           # Linux / Git Bash Agent 稳定入口
  bin/rmg.ps1                       # Windows PowerShell 管理入口
  bin/start-manager.ps1[或 .sh]      # 显式启动本地 daemon
  bin/Open remote-mng.cmd           # Windows 双击启动并打开状态页
  bin/open-console.sh               # Linux / WSL 启动并打开状态页
  versions/0.4.0/rmg[.exe]
  versions/0.4.0/_internal/...
  versions/0.4.0/rmg-release.json
```

Skill 的包装脚本绑定稳定入口及完整参数。Windows Agent 使用 Git Bash → Skill → `bin/rmg` → 版本目录 `rmg.exe`，直接保留空参数、引号、反斜杠和标准输入 JSON，避免 PowerShell 5.1 `-File` 对独立 `-` 参数的解析限制；Windows Claude Code 需有 Git Bash。PowerShell 管理入口对传给 native 程序的参数实现 CRT 引号序列化。源码安装则绑定持久 Python 环境，直接从独立包执行 `setup` 时绑定其可执行文件。daemon 启动会识别这两种运行形式，不会把 frozen `rmg.exe` 当成 Python 解释器运行 `-m remote_mng`。

## 日常更新

进入 0.4.0 后，开发者直接向 Claude Code 说“更新 remote-mng”。Agent 使用 `rmg update status --json` 核对实际安装，再用 `rmg update check --json` 固定发布计划，提交 `rmg update --plan-id PLAN_ID --json`，按返回的更新 ID 查询。网页提供同一更新记录及独立进度页；打开网页或读取状态不会自动连接 GitHub 查新。

统一更新支持受管独立包和能够验证归属的普通 `uv tool install` 环境；可编辑源码、未知 pip 环境或无法核验的自定义环境会报告阻碍。更新前检查活动状态、兼容性、Skill 完整性和最终执行进程的独立创建能力，通过后才停止空闲管理器并切换版本。`blocked` 表示保留现状等待处理，不能报告更新成功；即使从普通终端发起也不能跳过预检。完整流程、显式恢复、个人约定和支持边界见 [更新指南](updates.md)。

正式 0.3.0 尚无 `update` 命令，需要先检查并停止旧管理器，使用一次[发行包安装流程](#首次安装)进入 0.4.0；源码安装则按 [旧版迁移步骤](claude-skill.md#升级旧版本) 重装。不要向 0.3.0 的旧 CLI 直接发送 `update`。

以下 `distribution` 命令保留给安装脚本、离线资产和显式维护使用，仍维持自身严格的停止管理器前置条件。不要把底层安装器的操作顺序与日常更新流程混用。

## 底层固定版本下载、升级与回退

已安装 `rmg` 时可以让 Agent 使用已认证 `gh` 下载一个精确命名的私有制品：

```sh
rmg distribution fetch --repository LX-Cel/remote-mng --tag v0.4.0 \
  --asset remote-mng-0.4.0-linux-x86_64.zip --sha256 '<64位摘要>' \
  --destination ./remote-mng-0.4.0-linux-x86_64.zip --json
```

不接受 `latest`、通配资产名或缺省摘要。下载不会自动安装。安装/升级始终是明确操作，不在任务中途自动换版本。

升级流程：检查当前 daemon 和 Skill → SHA256 与安全文件清单 → 独立版本目录 → 版本/平台/协议与资源检查 → 隔离目录安装 Skill、启动 daemon、服务网页、正常停止并等待进程释放文件 → 再次检查使用状态 → 锁住 daemon 启动 → 切换稳定入口并更新受管 Skill。任何失败都不会主动终止用户 daemon，也不会自动删除旧版本。

这里的候选健康检查由安装器持有测试 daemon 的进程句柄，结束后显式停止并等待它退出；它验证候选功能，不单独证明常驻管理器能脱离更新进程独立运行。统一更新在切换前另外验证该能力。`distribution status` 只描述受管独立安装目录；检查源码或 uv 安装时使用 `update status`，不要把没有受管目录误判为工具未安装。

只要 daemon 仍在运行，安装器就会拒绝切换，并报告活动会话数。先检查任务和会话，在适合断开的时候明确停止 daemon，再重试。普通前台会话不能通过升级恢复；已提交的远端 durable job 继续保留原目录和 job ID，升级后应查询这些 ID。

```sh
rmg distribution status --json
rmg distribution rollback --json
rmg distribution rollback --to-version 0.3.0 --json
```

上例 `0.3.0` 是已经保留在本机的历史回退目标，不是首次安装推荐版本；只有实际保留并通过兼容检查时才能使用。

回退前再次验证候选文件与运行健康。仅支持完全匹配的 daemon、数据库、helper、Skill 协议；未知数据库结构或协议变化时拒绝，不自动迁移。程序回退不等于数据库恢复，本轮没有不可逆数据迁移。状态 SQLite、日志和远端 helper 都不随程序切换复制、删除或改写。激活失败会恢复原版本指针，并在没有并发编辑的前提下恢复原受管 Skill 的精确字节。

## 构建和验证

在各目标平台分别执行：

```sh
uv sync --frozen --group bundle
uv run --frozen python scripts/build_release.py
uv run --frozen python scripts/smoke_release.py --build-result dist/release/build-result.json
```

构建生成 ZIP、通用 wheel、SHA256、逐文件清单、发布元数据和 `build-result.json`。上方不带前驱参数的 smoke 验证当前包的兼容重装；正式跨版本验收需为 `smoke_release.py` 添加 `--previous-artifact` 和 `--previous-sha256`，并运行 `smoke_update.py --build-result ... --previous-build-result ...` 及 `smoke_source_update.py --build-result ...`。前驱必须来自真实保留的版本，uv 特定测试夹具另按验收记录标识。

smoke 的 Python 充当测试服务器与断言工具；被测 CLI、后台 daemon、Skill 绑定来自独立包，使用隔离安装和状态目录。分发层覆盖含中文、空格与单引号的路径、SSH 返回码、SFTP、Telnet 去重和兼容回退；Linux 还运行真实 detached helper 作业并核对副作用恰好一次。worker 层另验证真实更新进程、管理器停启、进度页和离线回退，GitHub 传输使用固定本地资产夹具，不替换安装器或管理器。边界与实际结果见 [0.4 验收记录](v04-validation.md)。

`.github/workflows/release.yml` 配置在 Windows 和 Ubuntu 22.04 构建，默认要求真实前驱的分发升级、worker 更新和 uv 更新／回退全部通过。手动工作流仅保存 Actions artifacts，tag 推送在两平台成功后创建正式 release，并附带安装脚本及摘要；仓库继续保持私有。[0.4.0 本轮发行验收](https://github.com/LX-Cel/remote-mng/actions/runs/34837898053) 的 Linux 全部通过、Windows 未通过，正式自动发布未执行；当前提供的是明确标注限制的 [v0.4.0 预发布](https://github.com/LX-Cel/remote-mng/releases/tag/v0.4.0)。Linux 包来自通过验收的 CI，Windows 包来自同提交的干净本机构建及已记录的分发/保护验收，不是失败的 Windows CI 制品。完整来源与摘要见 [验收记录](v04-validation.md)。

0.4.0 本地 Windows 分发层验收通过；当前 Codex 宿主中的 worker 自动更新受到嵌套 Job Object 限制，已验证会提前报告阻碍并保留原状态，结果标记为 `passed_blocked_safely`，不能称为自动更新成功。发布 CI 不使用这个诊断模式替代完整成功条件。普通终端也须经过实际检查，预启动不能保证后续更新进程具有相同权限。

历史 0.3.0 验收中，GitHub 托管 Windows runner 的独立 daemon 启动被拒绝；该版 Windows 资产在普通本机环境从固定提交构建并通过当时的完整分发 smoke，Linux 资产来自通过 smoke 的 Ubuntu 22.04 CI。具体来源、摘要与历史检查见 [0.3 交付总表](v03-validation.md)，不据此推断 0.4 发布流水线已经通过。
