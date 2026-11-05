# LunarShroud

**简体中文** | [English](README.en.md)

LunarShroud 是一个面向 Windows 的**本地 AI 聊天应用**：自带 llama.cpp 构建与 GGUF 模型，无账号、不联网、所有数据留在本机。使用 Tauri、FastAPI 与原生 JavaScript 构建，内置参数编辑、预设、多实例、实时日志、资源监控、启动历史、原生流式 Chat（可回退到 llama-server WebUI）、本地知识库/RAG、本地备份与托盘生命周期。

> 当前版本：`1.0.0`。**发布通道是便携包**（`LunarShroud-1.0.0-portable-x64.zip`，解压即用）；本版不提供 NSIS 安装包。产物**未做代码签名**，下载后请按发布页给出的 SHA-256 校验再运行；详见 [docs/RELEASE.md](docs/RELEASE.md)。
>
> 版本定位：`1.0.0` 表示**功能完整、可日常使用**，不是"生产级稳定"的无保留结论。
> **8 小时长稳与多硬件矩阵尚未完成**（4 小时真实模型 soak 已通过），未签名的便携包
> 仍会触发 SmartScreen 未知发布者提示。这两条是已披露的限制，见下方「已知限制」。

## 主要能力

- llama-server 参数可视化编辑：面板收录常用项，按**本机构建**自适应显隐（`--help` 探测是权威，不是硬编码清单），长尾参数走「附加命令行参数」
- 「用途」一键配方：对话生成 / 嵌入服务 / 重排序 / 只读工具 / Agent 全功能（只写意图，不写调优数字）
- 本地知识库/RAG：文本、HTML、PDF、DOCX 与扫描版 PDF（GLM-OCR）在本机建索引；Chat 可选择知识库，回答带页码引用
- RAG 服务组件齐备：`/v1/embeddings`、`/rerank`、提示词缓存、内置工具与 MCP，详见 [docs/RAG.md](docs/RAG.md) 与 [docs/KNOWLEDGE_BASE.md](docs/KNOWLEDGE_BASE.md)
- 多实例启动、停止、重启、端口分配和状态监控
- 有界实时日志、搜索、过滤和下载
- CPU、内存和 NVIDIA GPU 监控
- 模型库：GGUF 能力探测（原生上下文/MTP/mmproj）、显存预估、配套文件识别，以及可选的 `model.yaml` / `manifest.json` 清单编辑
- 预设导入、导出、克隆、对比和启动历史
- 原生 Chat：本地会话（`chats.db`）、SSE 流式、停止/重新生成、消息编辑与分支、上下文占用、本地文本附件、提示词库、会话搜索、Markdown 导出；WebUI 作为回退
- **Chat 右栏双视图**：`Configuration`（实例/知识库/系统提示/采样/推理/上下文）与 `Integrations`（智能体与工具状态、会话级工具目录授权、常驻 embedding/rerank/OCR 服务、本地 API 网关），宽度可拖拽
- **一键停止当前模型**：顶栏模型胶囊右侧的停止按钮，无需切到仪表盘
- **本地 API 服务**：独立端口上的 OpenAI 兼容网关，可开关、设端口与 Bearer 密钥、限制跨域来源，并实时查看 / 导出接口请求日志
- **设置页 `硬件` / `开发者` 分类**：前者显示 CPU/内存/GPU/显存/温度与 GPU 进程，后者显示版本、平台、数据与日志路径、运行环境探测状态
- 本机数据备份/恢复：配置、预设、历史与聊天（含附件和提示词）快照，SHA-256 校验，恢复在下次启动应用
- 托盘图标、可选关闭到托盘、显式退出；Ctrl+K 命令面板

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
   | AMD 独显（RX 6000/7000 等，官方 ROCm 支持列表内） | `bin-win-rocm-*-x64` |
   | AMD 其它（含核显） | `bin-win-vulkan-x64` |
   | Intel 核显 / Arc / NPU | `bin-win-openvino-*-x64` |
   | 只有 CPU | `bin-win-cpu-x64` |

   ROCm 包只对 AMD 官方支持列表内的独显生效（gfx9/gfx10 系列）；核显与未列入
   列表的型号在 `--list-devices` 里为空，请改用 Vulkan 包。OpenVINO 包走 Intel
   的 CPU/GPU 插件，设备名以 `OPENVINO` 开头（其中 CPU 设备通常排第一）。
   注册后到「运行环境」页可以看到这份构建在**本机实际枚举到的设备**，参数面板的
   「计算设备」（`--device`）就填那里列出的名字。

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
Push-Location frontend
node --test tests\*.test.mjs
Get-ChildItem js -File | Where-Object Extension -in '.js', '.mjs' | ForEach-Object { node --check $_.FullName }
# 浏览器核心路径、axe 可访问性与首屏性能门禁（需先启动后端）
npm ci
npm run test:browser
Pop-Location
cargo test --manifest-path src-tauri\Cargo.toml --locked
```

当前工作树基线（2026-11-05）：**925 个 Python 测试（924 通过 + 1 跳过）**、117 个前端单元测试、全部前端 JavaScript 语法检查通过；产品代码覆盖率 **84.75%**（门禁 ≥75%）。干净检出里没有 `study/` 下那几份预编译 llama.cpp 包，相关的 6 个多后端用例会跳过（**918 通过 + 7 跳过**），这是预期行为。Rust 测试 **23 个全部通过（0 失败）**，但**必须把 `CARGO_TARGET_DIR` 指到仓库之外的短路径**（例如盘符根下的短目录）：默认的 `src-tauri/target/` 下 `ring` 的 C 编译会因路径定位问题失败；与 MSVC 安装、SDK 和源码均无关。测试数量随回归测试增长，门禁始终是 **0 失败 + 覆盖率 ≥75%**，以实际运行为准。测试不替代真实模型和长时间压力测试。

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

文档导航见 [docs/README.md](docs/README.md)。

## 已知限制

- 仅支持并验证 Windows 10/11；macOS 和 Linux 尚未支持。
- 便携包未代码签名，Windows SmartScreen 可能提示未知发布者。
- 1.0.0 只发布了便携包；NSIS 安装包可用 `cargo tauri build` 自行产出，但本版未做安装/卸载往返验证。
- GPU 监控优先使用 NVML（`nvidia-ml-py`），库或驱动不可用时回退 `nvidia-smi`。
- 不同 llama.cpp 版本的参数可能变化，升级后应检查生成命令。
- llama.cpp 默认 `--cache-ram 8192` 会启用宿主侧提示词缓存。实测 4B 模型运行 10 分钟后 llama-server 内存从 5.2 GB 升至 11.4 GB 并平台化；这不是启动器泄漏。内存较小的机器请把 `--cache-ram` 调低或设为 0（代价是重复提示词不再复用）。
- 实例端口默认不设 API 密钥。Windows 回环端口不按用户隔离，共享机器上的其他本地进程可以直接调用模型；共享环境请在参数中设置 `api_key`。
- 实例端口范围（默认 8080–8090，共 11 个）由模型实例与知识库的嵌入/重排/OCR 服务**共用**：同时运行多个实例并建知识库索引时可能耗尽，表现为"端口范围内没有可用端口"；在设置里放宽范围即可。
- OpenVINO 构建的设备不走 `--device`，而是 `GGML_OPENVINO_DEVICE` 环境变量（启动器会自动翻译，一次只能选一个设备）；实测部分显卡上其 GPU 插件加载模型后会崩溃（0xC0000409），遇到就换 `OPENVINO0`（CPU）或改用 Vulkan/CUDA 构建。
- ROCm 构建在没有受支持 AMD 独显的机器上会**静默退回 CPU 推理**并照常启动（实测生成速度慢一个数量级）；启动器会在实例卡片上给出提示，但不会阻止启动。
- Windows 上总路径超过 260 个字符的模型/草稿/mmproj 无法被 llama.cpp 打开；启动前检查会报 `path_too_long`，请把文件移到更短的目录。
- llama.cpp v0.7.0：嵌入模型在**单槽位且未开启嵌入端点**时会触发上游断言而崩溃（`GGML_ASSERT(n_outputs_max ...)`）。勾选「启用嵌入端点」或把并行槽位设为 2 以上即可规避；启动器会把这条断言翻译成可行动的提示。
- Chat 默认使用原生模式；切到 WebUI 模式后会把 llama-server Web UI 嵌在沙箱 iframe 里（脚本、表单、同源），沙箱不含下载权限，因此 v0.7.0 模型管理器的下载功能请点工具栏的「在浏览器中打开」。
- 聊天、单会话消息和提示词库达到固定上限（500）时会**明确拒绝新建**，不会自动删除旧内容；请先导出或手动删除。启动历史仍按配置的 `history_max_records` 保留最近记录。
- 命令行备份/恢复工具要求应用与 `backend.exe` 均已关闭；备份清单统一为 UTF-8。应用内备份可在运行中通过 SQLite 快照完成。
- 恢复前会创建 `pre-restore-*` 快照；如果恢复过程中失败，当前版本需要从该快照手动恢复，尚未实现自动回滚。
- 启动历史按 `history_max_records` 保留最近记录；超过上限会裁剪最旧诊断记录，不影响聊天/预设。
- 4 小时真实模型 soak 已通过；**8 小时长稳与多硬件矩阵仍未完成**，因此 `1.0.0` 不宣称"生产级稳定"。这两项是发布前明确披露的未验证项。

## 参与和许可

提交改动前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。项目使用 [MIT License](LICENSE)，第三方说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

当前依赖安全审计基线见 [DEPENDENCY_AUDIT.md](DEPENDENCY_AUDIT.md)。

## 致谢与来源

### AI 辅助开发

本项目的开发过程由 **DeepSeek-V4.1-Flash**（`deepseek-v4.1-flash-10-17`）作为编码代理全程辅助：缺陷定位、回归测试编写、文档整理与发布验证均有人机协作的环节。所有由辅助工具提出的结论都经过实际复现才被采纳——本仓库的 `AGENTS.md` §6「验证纪律」与 `CHANGELOG.md` 中标注"实测/measured"的条目，就是这条约定的产物。

代码本身不包含任何联网推理调用：运行时无账号、无遥测、不访问网络。

### 前端审美规范

界面的设计令牌、间距刻度、层级与配色规则参考了 **frontend-aesthetics** 插件：

<https://github.com/1LunarVeil/frontend-aesthetics.git>

该插件提供的是**规范与方法**（令牌层先行、对比度门槛、视觉层级检查清单），不是运行时依赖，也不进入发布产物。具体实现见 `frontend/css/tokens.css` 与 [docs/DESIGN_1.0.md](docs/DESIGN_1.0.md)。

### 开源项目

感谢 [llama.cpp](https://github.com/ggml-org/llama.cpp)、[Tauri](https://tauri.app/) 和 [FastAPI](https://fastapi.tiangolo.com/) 社区。
