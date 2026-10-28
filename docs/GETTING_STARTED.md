# 第一次使用 LunarShroud

这份文档面向**全新安装**的用户：从下载 llama.cpp 到第一次成功对话。
应用内也有一份同内容的「新手指南」页（侧边栏最后一项），其中带有会读取当前
配置的动态检查清单。

> LunarShroud 只是 llama.cpp 的桌面启动器。它**不打包、也不自动下载**
> `llama-server.exe` 和模型文件；这两样需要你准备。

## 五步开始

### 1. 安装启动器

- Windows 10/11，需要 **WebView2 Runtime**。Win11 通常自带；Win10 如果打开后
  窗口空白，请先安装 Microsoft Edge WebView2 Runtime。
- 安装版：运行 `LunarShroud_<version>_x64-setup.exe`。
- 便携版：解压后运行 `LunarShroud.exe`。**不要只复制一个 exe**：
  `backend\backend.exe` 必须与启动器在同一个目录里配套使用。

### 2. 获取 llama-server

打开 <https://github.com/ggml-org/llama.cpp/releases>，下载 Windows 包：

| 你的显卡 | 选择文件名里含 | 备注 |
|---|---|---|
| NVIDIA RTX 30/40/50 | `bin-win-cuda12.x-x64` | 推荐；需要 CUDA 运行库 |
| AMD / Intel 显卡 | `bin-win-vulkan-x64` | 驱动保持较新 |
| 只有 CPU | `bin-win-cpu-x64` | 慢，但兼容性最好 |

解压到一个固定目录（例如 `D:\llama`），里面应当有 `llama-server.exe`。

NVIDIA 用户还需要 CUDA 运行库：安装 NVIDIA 官网的 CUDA Toolkit 运行组件，或把
发布包附带的 `cudart64_*.dll` / `cublas64_*.dll` 放到 `llama-server.exe` 旁边。
**缺少这些 DLL 是最常见的启动失败原因之一。**

### 3. 准备模型

- 主模型：一个 `.gguf` 文件。8 GB 显卡建议从 `Q4_K_M` 起步。
- 视觉模型：还需要配套的 `mmproj-*.gguf`（填「多模态投影」，不要当成主模型）。
- 推测解码：草稿模型必须与主模型**词表一致**（同系列/同转换来源）。
- 确认文件已经下载完整：只有几 KB、或者复制到一半的 `.gguf` 无法加载。

### 4. 在设置里指定路径

设置 → **路径**：

1. `llama-server 路径`：选择刚解压的 `llama-server.exe`。
2. `模型扫描目录`：选择放 `.gguf` 的目录（可留空，之后手动填完整路径）。

启动器**故意不给 llama-server 默认路径**：有默认值就等于每次启动都自动执行
安装目录里一个用户可写的文件。这是安全设计，不是漏做。

设置 → **服务与端口**：一般保持默认；端口留空启动实例时会自动分配。

### 5. 第一次启动

- 模型库 → 选中模型 → **直接启动**；或仪表盘 → **启动新实例**。
- 启动前会跑一次**启动前检查**：缺路径、模型不是 GGUF、嵌入模型没开嵌入端点、
  显存不足、端口被占用等问题会直接列出。错误项会阻止启动；警告项可以选择
  「仍然启动」。
- 第一次加载大模型可能需要几十秒到几分钟，状态是「启动中」。就绪后顶部显示
  运行中，Chat 页默认使用**原生流式对话**；需要上游模型管理器或高级界面时可
  切到 WebUI 模式（仍然嵌在沙箱 iframe 中）。
- 失败时实例卡片上会出现 **「为什么没启动？」**，点开会给出原因分类和修复建议。

## 这些操作最容易出错

1. 把 **mmproj**（视觉投影文件）当成主模型选中。
2. 只选中了一个**目录**，或选中了 `LunarShroud.exe` 而不是 `llama-server.exe`。
3. NVIDIA 用了 CUDA 构建却没装 CUDA 运行库（缺 `cudart`/`cublas`）。
4. 模型还在下载/复制中就开始启动；`.gguf` 只有几 KB 或尾部缺失。
5. 上下文/并行槽位开得太大：8 GB 显卡上 32K 上下文 + 多槽位很容易在首次推理时 OOM。
6. 嵌入模型（如 nomic-embed）不开「启用嵌入端点」，且并行槽位为 1 —— 部分
   llama.cpp 版本会触发 `GGML_ASSERT(n_outputs_max ...)`。模型库会对这类模型
   显示「嵌入模型」徽章，直接启动会自动补上 `--embeddings`。
7. 在「附加命令行参数」里写 `--host` / `--port` / `--api-key` / `--path` /
   `--log-file`：这些由启动器代管，会被拒绝。
8. 把监听地址改成 `0.0.0.0` 却不设 API 密钥：启动器会拒绝（安全边界）。
9. 沿用旧的 `--n-gpu-layers 99`：新版本建议留空（自动），显存不足时 99 会让
   llama.cpp 停止自适应。
10. 端口被别的程序占用，或两个实例手动指定了同一个端口。端口自动分配是
    “探测 → 释放 → 子进程绑定”，极小概率会被第三方进程抢走；重试会换端口。
11. 命令预览里的 `--n-cpu-moe 0` / `--n-cpu-ffn 0` 是**显式表示不卸载**，不是
    错误，也不是启动器在"乱加参数"；上游 `--help` 没有声明这两个选项的默认值，
    所以启动器不替它猜。
12. 内存较小的机器沿用 llama.cpp 默认的 `--cache-ram 8192`。实测 4B 模型运行
    10 分钟后 llama-server 的宿主内存从 5.2 GB 涨到 11.4 GB 后平台化（这是
    提示词缓存，不是泄漏）。16 GB 以下内存建议在参数面板把 `--cache-ram` 调到
    2048 或 0；设为 0 会关闭缓存，代价是重复提示词不再复用。
13. 模型、草稿模型或 mmproj 放在很深的目录里，总路径超过 260 个字符。
    llama.cpp 在 Windows 上打不开这种路径；启动前检查会报
    「模型路径超过 Windows 限制」，把文件移到更短的目录即可。

## 启动失败排查表

| 现象 | 最可能的原因 | 怎么办 |
|---|---|---|
| 窗口空白 / 打不开 | 缺 WebView2 Runtime | 安装 Microsoft Edge WebView2 Runtime 后重启 |
| 提示找不到 llama-server | 路径为空或文件被移动 | 设置 → 路径重新选择；便携版确认 `backend\backend.exe` 在旁边 |
| `0xC0000135` / 缺少 DLL | CUDA 运行库缺失，或选错 GPU 构建 | 装 CUDA 运行组件；或换 Vulkan/CPU 构建 |
| `CUDA error` / `no CUDA-capable device` | 驱动过旧、显卡不受支持、构建不匹配 | 更新 NVIDIA 驱动；确认下载的是对应 CUDA 版本的包 |
| `out of memory` / `failed to allocate` | 显存或内存不足 | 降低上下文、并行槽位、GPU 层数；关闭其他占显存程序 |
| `error loading model` / `invalid magic` | 不是 GGUF、下载不完整、构建太旧 | 重新下载完整文件；升级 llama.cpp；确认架构被支持 |
| `GGML_ASSERT(n_outputs_max ...)` | 嵌入模型 + 单槽位 + 未开嵌入端点 | 勾选「启用嵌入端点」，或把并行槽位设为 2 以上 |
| 草稿模型相关崩溃/警告 | 草稿模型与主模型词表不一致 | 换同系列词表的草稿模型，或不启用推测解码 |
| 启动超时但日志在走 | 模型太大/磁盘慢/首次加载 | 等待；把启动超时调大；检查端口与日志 |
| `failed to open GGUF ... (No such file or directory)`，但文件确实存在 | 路径超过 Windows 260 字符限制 | 把模型/草稿/mmproj 移到更短的目录；启动前检查会提前报 `path_too_long` |
| 端口被占用 | 别的进程或另一个实例占着 | 端口留空自动分配，或换一个端口 |
| `unknown argument` / `invalid argument` | 本机构建不支持该参数，或 `extra_args` 写错 | 看参数面板的「已忽略」提示；清空 `extra_args` 重试 |
| 能运行但第一次对话就崩 | 显存余量不足（计算缓冲到推理时才申请） | 降上下文/槽位/层数；看显存预估是否标红 |

## 日志与数据在哪

- 安装版：`%APPDATA%\LunarShroud\`
  - `config.json`、`history.db`
  - `logs\backend.log`（后端/实例生命周期）
  - `logs\launcher.log`（外壳监管与重启记录）
- 源码运行：仓库下的 `data\`。

反馈问题时请附上 `backend.log` 的最后几十行。启动器已经把 Windows 异常退出码
翻译和最后一条致命日志写进实例卡片、历史详情与日志页。

### 备份与恢复用户数据

`config.json`、`history.db`、`chats.db`（聊天、附件、提示词）和 `presets\`
都在 `%APPDATA%\LunarShroud\`。可以在 **设置 → 数据备份** 创建、验证和恢复
本机备份（恢复在下次启动应用）；也可以关闭启动器后复制整个目录，或使用随
仓库提供的工具（需要 Windows PowerShell 5.1 或更新版本）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\Backup-UserData.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\Restore-UserData.ps1 `
    -BackupDirectory .\LunarShroud-backup-<时间戳>
```

备份目录带 `SHA256SUMS.txt`；恢复前会先校验，校验失败不会覆盖当前数据，并把
当前目录改名为 `LunarShroud.pre-restore-<时间戳>` 作为回退点。启动器运行时
工具会拒绝操作（除非显式 `-Force`，不推荐）。

## 安全边界（请勿绕过）

- 管理 API 只监听 `127.0.0.1`；非回环监听必须配 API 密钥。
- 会话令牌只经环境变量交给后端，不会写入日志，也不会传给 llama-server 子进程。
- 实例端口默认也只听回环，但**不设 API 密钥**。Windows 的回环端口不按用户
  隔离：共享机器上的其他本地进程可以直接调用模型。共享环境请在参数里设置
  `api_key`。
- 不要把启动器暴露到公网。

## 相关文档

- [BUILDING.md](BUILDING.md) — 从源码构建
- [RELEASE.md](RELEASE.md) — 发布与校验
- [RISK_ASSESSMENT.md](RISK_ASSESSMENT.md) — 剩余风险与 beta/1.0 判断
- [SECURITY_MODEL.md](SECURITY_MODEL.md) — 威胁模型
- [RAG.md](RAG.md) — 嵌入 / 重排 / 工具与 MCP
