# 依赖版本与许可证记录

下表的运行时依赖在 0.3 继续沿用已锁定版本；0.3 新增独立的 `bundle` 构建依赖组（PyInstaller 与其构建依赖），不改变 Python 运行时业务依赖。版本以 `uv.lock` 和 `runtime-info` 为准。许可证优先记录 `License-Expression`，没有该字段时记录 `License` 原文。项目链接来自包自己的 `Project-URL` 或 `Home-page` 元数据。

本轮主任务已经对包含传递依赖的环境运行 `pip-audit`，结果通过。本项目自身采用 [MIT License](../LICENSE)，第三方依赖仍按各自许可证分发。漏洞数据库审计与许可证核查是不同事项；本表不作许可证兼容性、分发义务或法律结论。随依赖包提供的完整许可证、版权和 NOTICE 文件仍是分发时需要核对的材料。

## 直接运行时依赖

| 包 / 官方文档 | 锁定版本 / 发布文件 | 项目用途 | 许可证元数据 |
| --- | --- | --- | --- |
| [AsyncSSH](https://asyncssh.readthedocs.io/) | [2.24.0](https://pypi.org/project/asyncssh/2.24.0/#files) | SSH 连接、PTY、命令通道、SFTP 和遗留 SCP | `EPL-2.0 OR GPL-2.0-or-later` |
| [telnetlib3](https://telnetlib3.readthedocs.io/) | [4.0.5](https://pypi.org/project/telnetlib3/4.0.5/#files) | Telnet 协商、文本流与终端连接 | `ISC` |
| [aiohttp](https://docs.aiohttp.org/) | [3.14.3](https://pypi.org/project/aiohttp/3.14.3/#files) | CLI/MCP 与本地会话管理进程之间的 HTTP 通信 | `Apache-2.0 AND MIT` |
| [Pydantic](https://docs.pydantic.dev/) | [2.13.5](https://pypi.org/project/pydantic/2.13.5/#files) | 目标配置和参数验证 | `MIT` |
| [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) | [2.2.0](https://pypi.org/project/mcp/2.2.0/#files) | MCP stdio 协议服务及工具定义 | `MIT` |
| [filelock](https://py-filelock.readthedocs.io/) | [3.32.6](https://pypi.org/project/filelock/3.32.6/#files) | 本地单实例与配置写入互斥 | `MIT` |
| [regex 官方项目](https://github.com/mrabarnett/mrab-regex) | [2026.9.10](https://pypi.org/project/regex/2026.9.10/#files) | 带超时约束的提示符和输出模式匹配 | `Apache-2.0 AND CNRI-Python` |

AsyncSSH 的安装元数据明确记录 `EPL-2.0 OR GPL-2.0-or-later`，随 2.24.0 包提供的 `asyncssh-2.24.0.dist-info/licenses/LICENSE` 是完整的 Eclipse Public License 2.0。其[官方许可说明](https://asyncssh.readthedocs.io/en/latest/#license)描述了在满足 EPL 2.0 条件时使用 GPL 2.0 或更高版本作为次级许可证的安排。本记录保留项目自己的声明，不将其改写成 MIT、BSD 或无条件的许可选择结论。

telnetlib3 使用的是**已经通过本项目协议集成测试的 4.0.5**，依赖约束暂时排除 5.x；这里不声称 4.0.5 是上游最新版。其余版本同样表示本项目当前锁定结果，而非持续更新的版本推荐。

## 传递运行时依赖

以下是从锁文件中 `remote-mng` 的运行时依赖递归展开的传递依赖；用途描述其在依赖栈中的角色，不表示项目会主动启动该包的全部功能。Windows 专有依赖只在对应平台安装。包名链接指向其元数据提供的官方项目或文档。

| 包 | 锁定版本 | 用途 | 许可证元数据 |
| --- | --- | --- | --- |
| [aiohappyeyeballs](https://aiohappyeyeballs.readthedocs.io/) | 2.7.1 | aiohttp 网络连接地址选择 | `PSF-2.0` |
| [aiosignal](https://docs.aiosignal.org/) | 1.4.0 | aiohttp 异步信号 | `Apache 2.0` |
| [annotated-types](https://github.com/annotated-types/annotated-types) | 0.8.0 | Pydantic 类型约束标注 | `MIT` |
| [ansicon](https://github.com/Rockhopper-Technologies/ansicon) | 1.89.0 | Windows ANSI 控制台支持 | `MPLv2.0` |
| [anyio](https://anyio.readthedocs.io/en/latest/) | 4.15.1 | SDK 异步任务与 I/O 抽象 | `MIT` |
| [attrs](https://www.attrs.org/) | 26.1.0 | 数据对象与验证依赖 | `MIT` |
| [blessed](https://blessed.readthedocs.io/) | 1.49.0 | Windows 终端能力支持 | `MIT` |
| [cffi](https://cffi.readthedocs.io/) | 2.1.1 | cryptography 的 C 接口支持 | `MIT-0` |
| [click](https://click.palletsprojects.com/) | 8.5.0 | ASGI 服务器依赖的命令行框架 | `BSD-3-Clause` |
| [cryptography](https://cryptography.io/) | 50.0.1 | SSH 密码学实现 | `Apache-2.0 OR BSD-3-Clause` |
| [frozenlist](https://frozenlist.aio-libs.org/) | 1.8.0 | aiohttp 可冻结列表 | `Apache-2.0` |
| [h11](https://github.com/python-hyper/h11) | 0.16.0 | HTTP/1.1 协议实现 | `MIT` |
| [httpcore2](https://github.com/pydantic/httpx2/blob/main/src/httpcore2) | 2.12.0 | SDK HTTP 连接层 | `BSD-3-Clause` |
| [httpx2](https://github.com/pydantic/httpx2) | 2.12.0 | MCP SDK HTTP 客户端依赖 | `BSD-3-Clause` |
| [idna](https://github.com/kjd/idna) | 3.19 | 国际化域名编码 | `BSD-3-Clause` |
| [jinxed](https://github.com/Rockhopper-Technologies/jinxed) | 2.1.0 | Windows 终端接口 | `MPLv2.0` |
| [jsonschema](https://python-jsonschema.readthedocs.io/) | 4.26.0 | JSON Schema 验证 | `MIT` |
| [jsonschema-specifications](https://jsonschema-specifications.readthedocs.io/) | 2025.9.1 | JSON Schema 规范资源 | `MIT` |
| [mcp-types](https://github.com/modelcontextprotocol/python-sdk) | 2.2.0 | MCP 协议类型 | `MIT` |
| [multidict](https://multidict.aio-libs.org/) | 6.8.0 | HTTP 多值字典 | `Apache License 2.0` |
| [opentelemetry-api](https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api) | 1.44.0 | SDK 可观测性 API 依赖 | `Apache-2.0` |
| [propcache](https://propcache.readthedocs.io/) | 0.5.2 | 属性缓存 | `Apache-2.0` |
| [pycparser](https://github.com/eliben/pycparser) | 3.0 | CFFI 的 C 语法解析 | `BSD-3-Clause` |
| [pydantic-core](https://github.com/pydantic/pydantic/tree/main/pydantic-core) | 2.46.5 | Pydantic 核心验证实现 | `MIT` |
| [PyJWT](https://github.com/jpadilla/pyjwt) | 2.14.0 | SDK JWT 支持 | `MIT` |
| [python-multipart](https://kludex.github.io/python-multipart/) | 0.0.32 | SDK HTTP 表单解析依赖 | `Apache-2.0` |
| [pywin32](https://mhammond.github.io/pywin32/) | 312 | Windows 系统接口 | `PSF` |
| [referencing](https://referencing.readthedocs.io/) | 0.37.0 | JSON Schema 引用解析 | `MIT` |
| [rpds-py](https://rpds.readthedocs.io/) | 2026.6.3 | 持久数据结构 | `MIT` |
| [sse-starlette](https://github.com/sysid/sse-starlette) | 3.4.11 | SDK SSE HTTP 传输依赖 | `BSD-3-Clause` |
| [Starlette](https://starlette.dev/) | 1.6.0 | SDK ASGI HTTP 依赖 | `BSD-3-Clause` |
| [truststore](https://truststore.readthedocs.io/) | 0.10.4 | 系统 TLS 信任库接口 | `MIT` |
| [typing-extensions](https://typing-extensions.readthedocs.io/) | 4.16.0 | Python 类型兼容扩展 | `PSF-2.0` |
| [typing-inspection](https://pydantic.github.io/typing-inspection/dev/) | 0.4.4 | 类型标注检查 | `MIT` |
| [Uvicorn](https://uvicorn.dev/) | 0.52.4 | SDK ASGI 服务器依赖 | `BSD-3-Clause` |
| [wcwidth](https://github.com/jquast/wcwidth) | 0.8.3 | 终端字符显示宽度 | `MIT` |
| [yarl](https://yarl.aio-libs.org/) | 1.24.5 | URL 数据类型 | `Apache-2.0` |

共记录 7 个直接依赖和 37 个传递依赖，对应 Windows、WSL/Linux 的运行时依赖范围。锁文件还包含仅适用于 Emscripten 的 `httpx2-jsfetch==1.0` 条件分支；该平台不在本项目首版支持范围，本次未安装或核实它的许可证。构建与测试工具所在的 `dev` 依赖组没有混入上述运行时表格。

`ansicon` 和 `jinxed` 的分类元数据均进一步指明 Mozilla Public License 2.0，表格保留其 `License` 字段原文 `MPLv2.0`。`pywin32` 记录了多个组件级许可证文件，`jinxed` 另有 `LICENSE.ncurses`，`aiohttp` 另有 `vendor/llhttp/LICENSE`；单个顶层字段不能取代这些随包材料。

## 复核方式

锁定安装使用项目根目录下的命令：

```sh
uv sync --frozen
uv run pip-audit
```

可以直接检查单个包的原始元数据，例如：

```sh
uv run python -c "from importlib.metadata import metadata; m=metadata('asyncssh'); print(m['Version']); print(m.get('License-Expression') or m.get('License')); print(m.get_all('License-File')); print(m.get_all('Project-URL'))"
```

修改 `pyproject.toml` 或更新锁文件后，应重新核对版本、运行时依赖图、许可证元数据及随包许可文件。本记录是本轮交付快照。

2026-09-13 的 0.3 开发环境再次执行 `uv run pip-audit --progress-spinner off`，可审计依赖未发现已知漏洞；项目自身未发布到 PyPI，审计器明确跳过 remote-mng 本体。该结果不等于源代码安全审查，也不保证未知漏洞不存在。独立包保留依赖元数据与许可证资源，构建及实际包检查见 [发行包验证](distribution-v03.md)。
