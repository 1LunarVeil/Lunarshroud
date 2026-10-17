# LunarShroud

LunarShroud 是一个面向 Windows 的 llama.cpp 桌面启动器，使用 Tauri、FastAPI 和原生 JavaScript 构建。它提供参数编辑、预设、多实例、实时日志、资源监控、启动历史和 llama-server 内置 Chat UI。

> 当前版本：`0.9.0-beta.2`。已经完成进程生命周期与本地接口安全加固，但在标记 1.0 前仍需要多种 GPU、模型和 4–8 小时长稳测试。

## 主要能力

- llama-server 参数可视化编辑：面板收录常用项，按**本机构建**自适应显隐（`--help` 探测是权威，不是硬编码清单），长尾参数走「附加命令行参数」
- 「用途」一键配方：对话生成 / 嵌入服务 / 重排序 / 只读工具 / Agent 全功能（只写意图，不写调优数字）
- RAG 组件齐备：`/v1/embeddings`、`/rerank`、提示词缓存、内置工具与 MCP，详见 [docs/RAG.md](docs/RAG.md)
- 多实例启动、停止、重启、端口分配和状态监控
- 有界实时日志、搜索、过滤和下载
- CPU、内存和 NVIDIA GPU 监控
- 模型库：GGUF 能力探测（原生上下文/MTP/mmproj）、显存预估、配套文件识别
- 预设导入、导出、克隆、对比和启动历史
- 内嵌 llama-server Web UI

## 稳定性与安全边界

- 阻塞式进程、磁盘和 GPU 操作在线程池执行；日志、历史、扫描、请求体和前端 DOM 都有明确上限。
- Windows Job Object 托管 backend 和 llama-server 子树，启动器退出或崩溃时由系统回收。
- 打包版使用操作系统安全随机数生成会话令牌，REST/WebSocket 均需认证；管理 API 仅监听 `127.0.0.1`。
- 发布版只允许执行安装目录中的捆绑后端，不从工作目录或 PATH 回退执行 Python/可疑程序。
- 预设 ID 和静态文件路径做规范化与目录边界校验；配置、预设采用原子写入。
- API 密钥不会写入预设、历史、日志或 API 响应。由于 llama-server 接收命令行参数，运行时密钥仍可能被本机同权限用户从进程命令行看到。

完整威胁模型见 [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md)，漏洞报告流程见 [SECURITY.md](SECURITY.md)。

## 环境要求

运行预编译版本：Windows 10/11、WebView2 Runtime、用户自行准备的 `llama-server.exe` 和 GGUF 模型。NVIDIA GPU 仅在需要 GPU 指标时使用。


### 第一次启动：获取 llama-server

> 完整的新手步骤、常见错误操作和启动失败排查表见
> [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)；应用内「新手指南」页会读取
> 当前配置并给出动态检查清单。

本项目**不打包** `llama-server.exe`（它是独立的 llama.cpp 产物，且要按显卡分变体），
所以装好启动器后还需要一步：

1. 打开 llama.cpp 的发布页 <https://github.com/ggml-org/llama.cpp/releases>，下载最新的
   Windows 包，按你的显卡选：

   | 你的显卡 | 选文件名里含 |
   |---|---|
   | NVIDIA（RTX 30/40/50 等） | `bin-win-cuda12.x-x64`（推荐，另见第 3 步） |
   | AMD / Intel 显卡 | `bin-win-vulkan-x64` |
   | 只有 CPU | `bin-win-cpu-x64` |

2. 解压到一个**固定位置**（例如 `D:\llama`），解压后里面就有 `llama-server.exe`。
3. 用 NVIDIA 时需要 CUDA 运行库：装 NVIDIA 官网的 CUDA Toolkit 运行组件，或把发布包
   附带的 `cudart64_*.dll` / `cublas64_*.dll` 一并放进 `llama-server.exe` 所在目录。
4. 启动 LunarShroud → 设置 → 「llama-server 路径」→ 选中刚解压的那个 exe。

> 启动器**故意不给这个路径设默认值**。设了默认值，就等于每次启动都自动执行
> "某个安装目录里恰好叫这个名字的文件" —— 而那个目录通常用户可写，见
> docs/SECURITY_MODEL.md。代价就是上面这一步要你来做一次。

源码构建还需要：Python 3.11+、Node.js 22、Rust `1.97.1` MSVC、Tauri CLI `2.11.4`，以及 Visual Studio C++ Build Tools 的“Desktop development with C++”工作负载。详细步骤见 [docs/BUILDING.md](docs/BUILDING.md)。

## 快速开始

```powershell
.\setup.ps1
```

仅以源码开发方式运行后端时，必须显式打开不安全开发模式；它仍只监听回环地址：

```powershell
$env:LUNARSHROUD_ALLOW_INSECURE_DEV = '1'
.\.venv\Scripts\python.exe backend\main.py
# 浏览器打开 http://127.0.0.1:17520
```

不要在共享机器、远程主机或生产环境使用该开发开关。桌面打包版由 Tauri 自动注入私有会话令牌，不需要这个变量。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=backend --cov-config=.coveragerc --cov-report=term-missing --cov-fail-under=75
node --test frontend\tests\runtime.test.mjs frontend\tests\i18n.test.mjs
Get-ChildItem frontend\js -File | Where-Object Extension -in '.js', '.mjs' | ForEach-Object { node --check $_.FullName }
cargo test --manifest-path src-tauri\Cargo.toml --locked
```

当前源码基线（2026-10-18）：508 个 Python 测试、20 个前端测试、17 个 JavaScript 文件语法检查、14 个 Rust 测试；产品代码覆盖率 88.41%。测试数量随回归测试增长，门禁始终是 **0 失败 + 覆盖率 ≥75%**，以实际运行为准。测试不替代真实模型和长时间压力测试。当前验证状态见 [docs/STATUS.md](docs/STATUS.md)，未完成风险、证据与 beta/1.0 判断见 [docs/RISK_ASSESSMENT.md](docs/RISK_ASSESSMENT.md)。

## 构建安装包

```powershell
# 固定构建元数据：否则连续两次 PyInstaller 构建的哈希会不同
$env:SOURCE_DATE_EPOCH = '1790000000'
$env:PYTHONHASHSEED = '0'
Set-Location backend
..\.venv\Scripts\python.exe -m PyInstaller backend.spec --clean -y
Set-Location ..\src-tauri
# 把 cargo registry 的本机路径（含构建机用户名）改写成中性前缀，
# 避免公开安装包嵌出构建者身份 —— 每次构建都要带上这个 RUSTFLAGS
$env:RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE\.cargo\registry\src=cargo-registry"
cargo tauri build --ci -- --locked
```

NSIS 安装包输出到 `src-tauri/target/release/bundle/nsis/`。发布流程和校验清单见 [docs/RELEASE.md](docs/RELEASE.md)。

如需在构建工作区直接运行，请不要复用旧的根目录 EXE。构建完成后使用发布器生成匹配的启动器和侧车后端，并更新桌面快捷方式：

```powershell
.\tools\Publish-PortableRuntime.ps1 -Destination . -BackupDirectory .\tmp\portable-backups
```

## 项目结构

```text
backend/        FastAPI API、进程管理、存储和监控
frontend/       原生 JavaScript UI
src-tauri/      Tauri 主进程、会话握手和 Windows Job Object
backend/tests/  后端单元与 API 集成测试
frontend/tests/ 前端运行时单元测试
docs/           架构、构建、发布与安全说明
```

文档导航见 [docs/README.md](docs/README.md)，接手/部署说明见 [docs/HANDOVER.md](docs/HANDOVER.md)，完整工作记录见 [docs/HISTORY.md](docs/HISTORY.md)。

## 已知限制

- 仅支持并验证 Windows 10/11；macOS 和 Linux 尚未支持。
- 当前安装包未代码签名，Windows SmartScreen 可能提示未知发布者。
- GPU 监控优先使用 NVML（`nvidia-ml-py`），库或驱动不可用时回退 `nvidia-smi`。
- 不同 llama.cpp 版本的参数可能变化，升级后应检查生成命令。
- llama.cpp 默认 `--cache-ram 8192` 会启用宿主侧提示词缓存。实测 4B 模型运行 10 分钟后 llama-server 内存从 5.2 GB 升至 11.4 GB 并平台化；这不是启动器泄漏。内存较小的机器请把 `--cache-ram` 调低或设为 0（代价是重复提示词不再复用）。
- 实例端口默认不设 API 密钥。Windows 回环端口不按用户隔离，共享机器上的其他本地进程可以直接调用模型；共享环境请在参数中设置 `api_key`。
- Windows 上总路径超过 260 个字符的模型/草稿/mmproj 无法被 llama.cpp 打开；启动前检查会报 `path_too_long`，请把文件移到更短的目录。
- llama.cpp v0.7.0：嵌入模型在**单槽位且未开启嵌入端点**时会触发上游断言而崩溃（`GGML_ASSERT(n_outputs_max ...)`）。勾选「启用嵌入端点」或把并行槽位设为 2 以上即可规避；启动器会把这条断言翻译成可行动的提示。
- Chat 页把 llama-server Web UI 嵌在沙箱 iframe 里（脚本、表单、同源），沙箱不含下载权限，因此 v0.7.0 模型管理器的下载功能请点工具栏的「在浏览器中打开」。
- 尚未完成多硬件矩阵和 4–8 小时真实模型 soak test，因此版本仍为 beta。

## 参与和许可

提交改动前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。项目使用 [MIT License](LICENSE)，第三方说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

当前依赖安全审计基线见 [DEPENDENCY_AUDIT.md](DEPENDENCY_AUDIT.md)。

感谢 [llama.cpp](https://github.com/ggml-org/llama.cpp)、[Tauri](https://tauri.app/) 和 [FastAPI](https://fastapi.tiangolo.com/) 社区。
