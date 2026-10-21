# 安装与手动更新

**[0.8.0 预览版](https://github.com/LX-Cel/remote-mng/releases/tag/v0.8.0) 提供单文件离线安装器和重新设计的开发者控制台。** 下载对应系统的一个文件后，可直接带入内网安装和更新；联网脚本与历史包维护保留在高级说明中。

## 推荐：下载一个离线安装包

安装包内含程序及运行依赖、Claude Code Skill、远端 Linux helper 和安装逻辑。将对应本机系统的一个文件带入内网即可，安装过程不访问 GitHub、Python 软件源或其他外网下载地址，也不需要外部 ZIP、wheel 或下载器。

| 本机系统 | 离线安装包 | 运行方式 |
| --- | --- | --- |
| Windows x86_64 | [remote-mng-0.8.0-windows-x86_64-setup.exe](https://github.com/LX-Cel/remote-mng/releases/download/v0.8.0/remote-mng-0.8.0-windows-x86_64-setup.exe) | 双击 |
| Linux / WSL x86_64 | [remote-mng-0.8.0-linux-x86_64-setup.run](https://github.com/LX-Cel/remote-mng/releases/download/v0.8.0/remote-mng-0.8.0-linux-x86_64-setup.run) | 在当前环境中赋予执行权限后运行 |

Linux / WSL：

```sh
chmod +x ./remote-mng-0.8.0-linux-x86_64-setup.run
./remote-mng-0.8.0-linux-x86_64-setup.run
```

`.run` 是包含运行时的原生可执行文件，不是需要下载依赖的 Shell 脚本。安装器检查内含文件，安装到个人目录、同步 Skill 并启动管理器；默认不需要管理员权限，不改全局 PATH。只有最终验证通过才报告成功。

**离线更新：带入并运行新版安装包。** 它识别已有受管安装，检查活动，完成版本切换并保留任务、远端 job ID 及个人 `USER.md`。重复运行同版本用于核对已有安装，不会下载新版；旧包也不会自动将新版本降级。需要替换程序时，活动前台、正在执行的操作或托管 Skill 改动会明确阻止切换。

成功后新开 Claude Code 会话，对它说：

> 使用 remote-mng，检查本地安装并打开状态控制台，引导我配置第一台远程设备。

离线方式会保存在本地。后续 Agent 查询更新或打开控制台时，显示“通过新版离线安装包更新”，不会联网检查 GitHub。程序仍按任务需要连接你授权的远程设备；离线安装不等于远程 SSH/Telnet 工作不需要网络。

当前离线安装器支持新装及受管独立包的更新，**不支持旧 uv/Python 环境的离线迁移**。检测到这类绑定时会在联网和替换环境之前停止并说明原因；不要先卸载旧环境来绕过保护。确需维护旧环境时，使用已验证的在线更新路径，或另行规划环境迁移。

安装包应从可信的项目发布或公司内部制品源取得。带入内网及执行权限遵循公司的现有要求。Windows 宿主禁止独立进程创建时仍可能阻止管理器启动，安装包不会绕过这些限制；按实际结果处理，不能把文件已释放解释为已安装就绪。

## 可选：在线安装

<details>
<summary>展开：网络可用时，使用 install.ps1 / install.sh</summary>

在普通系统终端下载对应脚本，查看内容后运行。两个入口都自动完成选版、下载、摘要与文件检查、安装程序和 Claude Code Skill，以及启动本地管理器。公开仓库不要求 GitHub 登录，不需要预装 Python、uv、jq 或 GitHub CLI。

Windows PowerShell：

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/LX-Cel/remote-mng/206ec39503bc21a23733fd0fb2a27204dbb67d2a/install.ps1 -OutFile install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Linux / WSL：

```sh
curl -fL https://raw.githubusercontent.com/LX-Cel/remote-mng/206ec39503bc21a23733fd0fb2a27204dbb67d2a/install.sh -o install.sh
sh ./install.sh
```

默认优先选择支持新入口的正式发布；当前预览期若尚无兼容正式版，会明确提示并选择兼容预览。`-Preview` / `--preview` 可主动包含预览，指定精确版本属于高级用法。新入口不会把 GitHub 上的新提交直接作为可安装版本。

**手动更新时，重新运行已经下载的同一脚本。** 它识别现有安装并处理安全切换，不需要手动停止服务、卸载、重新安装和单独复制 Skill。日常直接向 Claude 说“更新 remote-mng”即可；Agent 的通道选择会随安装记录保存，见 [更新指南](updates.md)。

如果显示阻碍，按提示处理后再试；安装器不会强制断开普通前台、结束业务操作或覆盖被修改的 Skill。正在运行的 0.3 管理器缺少新版安全更新协议，需要你确认普通前台可以断开后再由 Agent 处理。Windows 宿主若不允许独立进程创建，仍会明确报告，不能保证换成安装脚本就自动解决。

在线脚本的 Linux/WSL 入口需要 `curl`、`awk`、Info-ZIP `unzip` 和 `sha256sum`；缺失时给出安装建议，不自动 sudo。这些是可选在线脚本的依赖，离线安装包不需要它们。

</details>

## 平台、目录和个人配置

| 环境 | 说明 |
| --- | --- |
| Windows x86_64 | 离线包双击运行；原生 Claude Code 还需已有的 Git Bash |
| WSL / Linux x86_64 | 离线包自带运行时；仍需兼容的 Linux / glibc，WSL 在自身环境中运行 Linux 包 |
| macOS / ARM | 当前未承诺独立发行包支持 |

默认程序目录为 Windows `%LOCALAPPDATA%/remote-mng` 或 Linux `~/.local/share/remote-mng`，状态默认在 `~/.remote-mng`，Skill 默认在 `~/.claude/skills/remote-mng`。Windows 和 WSL 分别安装，不共用管理器、认证环境和路径。

安装不改全局 PATH。Skill 绑定个人安装的稳定入口，所以 Agent 不依赖你记住程序位置。桌面和启动入口的实际结果以安装器报告为准；也可以让 Claude 打开控制台。原有独立包提供 Windows `bin/Open remote-mng.cmd`、Linux/WSL `bin/open-console.sh`。

个人约定保存在 `~/.claude/remote-mng/USER.md`；它位于托管 Skill 外，安装和更新不会改写。自定义 Claude 配置目录时跟随该目录。不要直接编辑托管的 `skills/remote-mng` 文件来保存个人规范。

<details>
<summary>高级：自定义离线安装目录或只安装</summary>

Windows `.exe` 和 Linux `.run` 使用相同参数；通常无需填写。更新时默认沿用能验证归属的原安装目录和状态。

| 参数 | 用途 |
| --- | --- |
| `--install-dir PATH` | 程序目录 |
| `--home PATH` | 状态目录 |
| `--claude-dir PATH` | Claude 配置目录 |
| `--no-start` | 仅安装，不主动启动管理器 |
| `--json` | 为 Agent 返回结构化结果 |

安装版本由包本身固定，无需 `--version` 或手填程序摘要。自定义临时目录需可写且允许执行，并有足够空间容纳解包内容；公司将临时目录挂载为 `noexec` 时，应使用获准的目录，不能绕过执行策略。

</details>

## 在线脚本的高级目录与版本选择

通常不用填写这些参数。隔离安装、固定版本或自定义 Claude 配置时才需要：

| 目的 | Windows | Linux / WSL |
| --- | --- | --- |
| 精确发布版本 | `-Version vX.Y.Z` | `--version vX.Y.Z` |
| 程序目录 | `-InstallDir PATH` | `--install-dir PATH` |
| 状态目录 | `-StateDir PATH` | `--home PATH` |
| Claude 配置目录 | `-ClaudeDir PATH` | `--claude-dir PATH` |
| 仅安装，不启动 | `-NoStart` | `--no-start` |
| 其他仓库 | `-Repository OWNER/REPO` | `--repository OWNER/REPO` |

精确版本必须实际存在且提供兼容制品。入口校验 GitHub 元数据及精确 SHA256，再检查 ZIP 路径和链接，校验通过后才执行候选安装程序；候选还会核对逐文件清单和运行条件。摘要从选中 release 获取，无需人工复制。

Linux 包依赖 glibc，清单记录构建基线；正式 CI 使用 Ubuntu 22.04 / glibc 2.35。本地 Ubuntu 24.04 的 glibc 2.39 构建不能当作 2.35 已验证。0.6 安装器采用 PyInstaller 单文件封装，内含完整独立包，运行时需有可写并允许执行的临时目录。安装后的版本目录仍需完整保留，包括 `_internal`，不能只替换 `rmg.exe`。

## 已发布的 0.4.0 和离线安装

<details>
<summary>展开：已有发布资产的校验与安装，适合 0.4.0、离线或显式维护</summary>

从公开 [GitHub Releases](https://github.com/LX-Cel/remote-mng/releases/tag/v0.4.0) 下载对应 ZIP、`.sha256` 和 `install-release.ps1` / `install-release.sh` 及其摘要。无需登录；阅读脚本并核对脚本摘要后运行。0.4.0 仍使用下列旧入口，不能使用根目录的新入口安装。

Windows PowerShell，在下载目录执行：

```powershell
$scriptHash = ((Get-Content -Raw ./install-release.ps1.sha256).Trim() -split '\s+')[0]
if ((Get-FileHash ./install-release.ps1 -Algorithm SHA256).Hash -ne $scriptHash) { throw 'Installer checksum mismatch' }
$archiveHash = ((Get-Content -Raw ./remote-mng-0.4.0-windows-x86_64.zip.sha256).Trim() -split '\s+')[0]
./install-release.ps1 -Archive ./remote-mng-0.4.0-windows-x86_64.zip -Sha256 $archiveHash
```

Linux / WSL，在下载目录执行：

```sh
sha256sum --check install-release.sh.sha256 && \
  sh ./install-release.sh ./remote-mng-0.4.0-linux-x86_64.zip \
  "$(cut -d ' ' -f 1 ./remote-mng-0.4.0-linux-x86_64.zip.sha256)"
```

旧安装器仍要求管理器已经显式停止；先让 Agent 核对普通前台、操作及作业 ID，在适合断开时再处理。它不会安全自动停止运行中的旧管理器。安装成功后默认尝试启动，完成后新开 Claude Code 会话。

离线安装也可以使用已有 `rmg` 的底层入口：

```sh
rmg distribution install ./ARTIFACT.zip --sha256 SHA256 --json
```

底层 `distribution` 不替代日常统一更新，仍有严格的停止管理器前置条件。检查受管安装用 `distribution status`，检查 uv 等实际安装方式用 `update status`；不能把没有独立目录解释成没有安装。

公开发布通过 HTTPS 下载；已认证 GitHub CLI 是私有仓库或明确选择私有发行源时的高级后备。不要把 token 写进 URL、脚本或对话。

</details>

## 源码与 uv 维护

<details>
<summary>展开：只供需要源码或维护 Python 环境的开发者</summary>

准备 Python 3.11+、Git 和 uv：

```sh
git clone --branch v0.4.0 https://github.com/LX-Cel/remote-mng.git
cd remote-mng
uv tool install .
rmg setup --start-daemon
```

普通 uv tool 安装可使用统一更新，开发环境与日常工具环境分别维护。0.5 的 bootstrap 会先为受支持的 uv 安装保存原环境快照，再进行更新；Windows、WSL 的官方 0.4 wheel 到 0.5 已通过真实验证并保留原 uv 目录。可编辑源码、未知 pip、自定义环境或无法核验的文件会明确阻碍，不自动覆盖；范围见 [0.5 验收记录](v05-validation.md)。

上述 uv 更新路径需要在线发行资源。0.6 离线安装器不会尝试联网补齐 uv 依赖或接管这类环境，不能将“本地有安装包”视为支持离线迁移。

从源码分支手工维护时，Git 更新源码不等于已更新工具环境或 Skill。多命令维护流程仅适合明确选择源码的开发者，见 [Skill 与旧版维护](claude-skill.md#源码手工维护)。

</details>

## 构建和验证

在各目标平台分别执行：

```sh
uv sync --frozen --group bundle
uv run --frozen python scripts/build_release.py
uv run --frozen python scripts/smoke_release.py --build-result dist/release/build-result.json
```

构建生成 ZIP、通用 wheel、SHA256、逐文件清单、发布元数据和 `build-result.json`。上方不带前驱参数的 smoke 验证当前包的兼容重装；正式跨版本验收需为 `smoke_release.py` 添加 `--previous-artifact` 和 `--previous-sha256`，并运行 `smoke_update.py --build-result ... --previous-build-result ...` 及 `smoke_source_update.py --build-result ...`。前驱必须来自真实保留的版本，uv 特定测试夹具另按验收记录标识。

smoke 的 Python 充当测试服务器与断言工具；被测 CLI、后台 daemon、Skill 绑定来自独立包，使用隔离安装和状态目录。分发层覆盖含中文、空格与单引号的路径、SSH 返回码、SFTP、Telnet 去重和兼容回退；Linux 还运行真实 detached helper 作业并核对副作用恰好一次。worker 层另验证真实更新进程、管理器停启、进度页和离线回退，GitHub 传输使用固定本地资产夹具，不替换安装器或管理器。边界与实际结果见 [0.4 验收记录](v04-validation.md)。

`.github/workflows/release.yml` 默认要求两平台的真实前驱分发升级、前台安装、单文件离线安装、worker 更新和 uv 更新／回退全部通过；手动工作流保存制品，tag 工作流在全部成功后正式发布。0.6 的 Ubuntu 22.04 发行验收全部通过，Windows 仍受宿主进程创建等条件限制，正式自动发布未执行。当前 [v0.6.0 预发布](https://github.com/LX-Cel/remote-mng/releases/tag/v0.6.0) 使用通过验收的 Linux CI 制品和经过完整本地离线安装、官方 0.5 升级及分发回退的 Windows 制品，未使用失败的 Windows CI 包。完整来源与摘要见 [0.6 验收记录](v06-validation.md)。

0.4.0 本地 Windows 分发层验收通过；当前 Codex 宿主中的 worker 自动更新受到嵌套 Job Object 限制，已验证会提前报告阻碍并保留原状态，结果标记为 `passed_blocked_safely`，不能称为自动更新成功。发布 CI 不使用这个诊断模式替代完整成功条件。普通终端也须经过实际检查，预启动不能保证后续更新进程具有相同权限。

历史 0.3.0 验收中，GitHub 托管 Windows runner 的独立 daemon 启动被拒绝；该版 Windows 资产在普通本机环境从固定提交构建并通过当时的完整分发 smoke，Linux 资产来自通过 smoke 的 Ubuntu 22.04 CI。具体来源、摘要与历史检查见 [0.3 交付总表](v03-validation.md)，不据此推断 0.4 发布流水线已经通过。
