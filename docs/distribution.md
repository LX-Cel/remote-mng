# 个人安装、独立发行包和升级

remote-mng 仍由 Python 实现。独立发行包把解释器、依赖、网页、远端 helper 和 Claude Skill 一起放在一个目录中，使用者无需安装 Python。Agent 通过已安装的 Skill 使用稳定入口；升级只切换入口指向的版本，不需要重新记命令。

源码/uv 安装继续支持。已在持久环境安装 `rmg` 后，在普通本地终端执行 `rmg setup --start-daemon --json`，一次完成 Skill 安装、本地 daemon 启动和 doctor。它不会连接远端或修改 Claude 的其他设置；已有 Skill 被修改时明确拒绝覆盖。去掉 `--start-daemon` 则只安装 Skill 与检查本地状态。`rmg runtime-info --json` 可以检查当前运行形式、版本、资源与密码学后端。

部分 Windows Agent 宿主禁止后台进程脱离其 Job Object；该环境下 Agent 无法自行启动常驻 daemon。首次安装应在 Claude Code 外的普通终端运行，安装脚本默认启动本地服务并报告实际结果。电脑重启后，Windows 用户双击安装目录 `bin/Open remote-mng.cmd`，Linux/WSL 用户运行 `bin/open-console.sh`，即可重新启动并打开状态页，无需记忆业务 CLI。工具不会绕过宿主限制、创建计划任务或系统服务；启动被拒绝时保留已安装的工具和 Skill，并明确报告原因。

## 独立包的范围

- 首轮构建与验收平台是 Windows x86_64 和 Linux x86_64；WSL 使用 Linux 包。未声明 macOS、ARM 或任意嵌入式 Linux 的本地客户端兼容性。
- Windows 发行包使用 Windows 10 及之后版本的系统接口。远端 Linux helper 不受客户端 Windows 架构影响。
- Linux 包依赖宿主 glibc。`rmg-release.json` 中的 `runtime_requirements.glibc` 记录构建系统版本；安装器拒绝低于该版本的系统。CI 在 Ubuntu 22.04 构建，使用 glibc 2.35 基线。本地 WSL Ubuntu 24.04 构建的验证包记录 glibc 2.39，不能直接把其测试结果当作 glibc 2.35 已验证。
- 采用 PyInstaller onedir，不是单个 Go 风格的可执行文件。必须保留整个版本目录；删除 `_internal` 会破坏运行。

## 首次安装

从有访问权限的 GitHub 固定 release 下载对应 ZIP 和 `.sha256`。仓库私有时使用自己的 `gh` 登录身份，不把 GitHub token 写入 remote-mng 配置。先核对 release 身份与完整 SHA256，再使用仓库内可检查的安装脚本。

Windows PowerShell：

```powershell
.\scripts\install-release.ps1 -Archive '.\remote-mng-0.3.0-windows-x86_64.zip' -Sha256 '<64位摘要>'
```

Linux/WSL（需要系统已有 `unzip` 和 `sha256sum`）：

```sh
sh scripts/install-release.sh ./remote-mng-0.3.0-linux-x86_64.zip '<64位摘要>'
```

脚本先校验 ZIP，然后运行其安装器；安装器再验证清单中的全部文件。成功后默认启动本地 daemon；需要仅安装时，Windows 添加 `-NoStart`，Linux 在摘要后添加 `--no-start`。也可以从已有 source 或 frozen `rmg` 执行以下仅安装命令，再按需要运行安装目录 `bin/start-manager.ps1` 或 `bin/start-manager.sh`：

```sh
rmg distribution install ./remote-mng-0.3.0-linux-x86_64.zip --sha256 '<64位摘要>' --json
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
  versions/0.3.0/rmg[.exe]
  versions/0.3.0/_internal/...
  versions/0.3.0/rmg-release.json
```

Skill 的包装脚本绑定稳定入口及完整参数。Windows Agent 使用 Git Bash → Skill → `bin/rmg` → 版本目录 `rmg.exe`，直接保留空参数、引号、反斜杠和标准输入 JSON，避免 PowerShell 5.1 `-File` 对独立 `-` 参数的解析限制；Windows Claude Code 需有 Git Bash。PowerShell 管理入口对传给 native 程序的参数实现 CRT 引号序列化。源码安装则绑定持久 Python 环境，直接从独立包执行 `setup` 时绑定其可执行文件。daemon 启动会识别这两种运行形式，不会把 frozen `rmg.exe` 当成 Python 解释器运行 `-m remote_mng`。

## 固定版本下载、升级与回退

已安装 `rmg` 时可以让 Agent 使用已认证 `gh` 下载一个精确命名的私有制品：

```sh
rmg distribution fetch --repository LX-Cel/remote-mng --tag v0.3.0 \
  --asset remote-mng-0.3.0-linux-x86_64.zip --sha256 '<64位摘要>' \
  --destination ./remote-mng-0.3.0-linux-x86_64.zip --json
```

不接受 `latest`、通配资产名或缺省摘要。下载不会自动安装。安装/升级始终是明确操作，不在任务中途自动换版本。

升级流程：检查当前 daemon 和 Skill → SHA256 与安全文件清单 → 独立版本目录 → 版本/平台/协议与资源检查 → 隔离目录安装 Skill、启动 daemon、服务网页、正常停止并等待进程释放文件 → 再次检查使用状态 → 锁住 daemon 启动 → 切换稳定入口并更新受管 Skill。任何失败都不会主动终止用户 daemon，也不会自动删除旧版本。

只要 daemon 仍在运行，安装器就会拒绝切换，并报告活动会话数。先检查任务和会话，在适合断开的时候明确停止 daemon，再重试。普通前台会话不能通过升级恢复；已提交的远端 durable job 继续保留原目录和 job ID，升级后应查询这些 ID。

```sh
rmg distribution status --json
rmg distribution rollback --json
rmg distribution rollback --to-version 0.3.0 --json
```

回退前再次验证候选文件与运行健康。仅支持完全匹配的 daemon、数据库、helper、Skill 协议；未知数据库结构或协议变化时拒绝，不自动迁移。程序回退不等于数据库恢复，本轮没有不可逆数据迁移。状态 SQLite、日志和远端 helper 都不随程序切换复制、删除或改写。激活失败会恢复原版本指针，并在没有并发编辑的前提下恢复原受管 Skill 的精确字节。

## 构建和验证

在各目标平台分别执行：

```sh
uv sync --frozen --group bundle
uv run --frozen python scripts/build_release.py
uv run --frozen python scripts/smoke_release.py --build-result dist/release/build-result.json
```

构建生成 ZIP、SHA256、逐文件清单和 `build-result.json`。smoke 的 Python 只充当测试服务器与断言工具；被测 CLI、后台 daemon、Skill 绑定全部来自独立包，工作目录在仓库外，PATH 移除开发 Python。测试使用含中文、空格与单引号的临时目录，覆盖安装、资源、SSH 返回码、SFTP、Telnet 去重、持久任务和兼容重装/回退；Linux 还运行真实 detached helper 作业并核对副作用恰好一次。多版本升级/回退与失败恢复的状态边界由 `tests/test_distribution.py` 独立验证。

`.github/workflows/release.yml` 在 Windows 和 Ubuntu 22.04 构建并执行上述 smoke。手动工作流仅保存 Actions artifacts，tag 推送在两平台成功后创建 release；仓库可见性保持原有设置。此文描述流水线实现，不把未运行的 GitHub Actions 当作通过记录。
