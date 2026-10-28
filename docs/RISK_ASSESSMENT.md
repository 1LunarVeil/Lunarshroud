# 剩余风险评估

评估对象：`0.9.0-beta.2` 当前工作树与 `src-tauri/target/release/` 候选对
（launcher `5C8BA375…`、backend `28136B22…`；根目录部署对仍为
`0E778DD0…` / `A1BAACAA…`）
评估日期：2026-10-21  
结论：**可以作为 beta 发布，但不能按 1.0 宣传。** 本轮能自行验证的风险已实测；
不能在本机闭环的风险（代码签名、8 小时长稳、多硬件矩阵、构建来源证明）仍
构成发布门槛，必须如实披露；4 小时真实模型长稳已于 2026-10-19 通过。

## 等级定义

- **概率**：在本项目预期的 Windows 10/11 个人电脑场景下发生的可能性。
- **影响**：低 = 可自行恢复；中 = 功能不可用或数据需要手工恢复；高 = 安全边界
  被击穿、系统被接管或大范围用户受影响。
- **剩余风险**：在本轮已做缓解之后仍然存在的部分，不把"文档写了"当作已消除。

## 风险登记表

| ID | 风险 | 概率 | 影响 | 现状与证据 | 剩余风险 |
|---|---|---|---|---|---|
| R1 | 安装包/启动器未代码签名，SmartScreen 与杀毒软件可能拦截 | 高 | 中 | 发布物只有 SHA-256；`docs/RELEASE.md` 要求用 `tools/verify_checksums.py` 校验 | 用户无法从操作系统层面确认发布者；AV 误报只能靠加白/上报 |
| R2 | 未完成 8 小时真实模型长稳，未覆盖多 GPU/CPU 厂商 | 中（4h 已验证，8h 未验证） | 高 | 2026-10-19 部署对真实 Qwen3.5-4B Q4_K_M 完成 **4 小时 soak**：7186/7186 请求成功、0 失败，p99 0.281 s；RSS 3178.8→2795.0 MB（斜率 -2.053 MB/min），句柄 221→271、线程 41→39；停止/端口/无残留/真实数据不变全部通过。自动化报告仅因 AC 前置检查在 12:00:07 读到 plugged=False 判 FAIL；Windows 电源事件显示 11:59:24–12:02:40 有两次电源变化，与约 3 分钟拔电窗口一致，16:03 电池 96% 且已接 AC，无睡眠/恢复事件，最大采样间隔 61.2 s，产品项通过（见 STATUS）。此前 15 分钟部分 soak 402/402、平均 0.242 s、RSS ~3.18 GB 平稳 | 8 小时长跑、多 GPU/CPU 厂商、驱动崩溃/功耗墙行为未知 |
| R3 | 没有 GitHub 构建来源证明（provenance attestation） | 高 | 中 | 只有 `SHA256SUMS.txt` 与发布说明中的摘要 | 无法证明安装包由公开源码在 CI 构建；供应链信任依赖发布者 |
| R4 | 自动分配端口的 probe→release→子进程 bind TOCTOU | 低 | 低 | `test_e2e_smoke.py` 覆盖分配与重试；本轮 8 个实例并发启动/停止，端口 8080–8087 全部唯一；第三方抢端口仍表现为明确的 bind 错误 | 极小概率首次启动失败，重试会换端口；未消除 |
| R5 | llama.cpp 默认 `--cache-ram 8192` 会让宿主内存随提示词缓存增长 | 中 | 中 | 本轮对照实测：默认 610 s 内 llama-server RSS 5.2→11.4 GB 后平台化；`--cache-ram 0` 时稳定在 3.21 GB（见下文） | 8 GB 内存机器可能被挤压；已写入使用指南，需用户按内存调低/设 0 |
| R6 | 本机其他进程/其他 Windows 用户可直接访问 llama-server 实例端口 | 中 | 中（共享机器） | 实例默认监听 `127.0.0.1`，但默认不设 `--api-key`；llama.cpp 自己会打印 “no API key is set” 警告 | 同机进程可调用模型、消耗 GPU；管理 API 仍受令牌保护。共享机器应在参数里设置 API 密钥 |
| R7 | 无自动更新，漏洞修复依赖用户手动换版 | 中 | 中 | 发布流程只产出安装包与校验和 | 旧版本可能长期停留在有已知问题的依赖上 |
| R8 | 卸载清理与安装位置注册表键 | 低 | 低 | 2026-10-20 直接复跑：安装/启动/卸载/清理全部通过；安装器内 launcher 是 NSIS 变体 `87B0B2E1…`（根便携版 `0E778DD0…`，仅差 3 字节 `BUNDLE_TYPE_VAR` `NSS`/`UNK`，已用测试钉住）；安装版后端 `A1BAACAA…`、`_internal` 757，401/404/HMAC/config 200、WM_CLOSE、端口释放、无后端残留；卸载 exit 0，安装目录（含 WebView2/log）移除，真实数据/快捷方式恢复。Add/Remove 条目仍**无法观测**：Python HKCU 探针通过，但 POSTINSTALL 钩子实测安装器为低完整性（`TEMP=...\Temp\Low`，`HKCU_WRITE_ERROR` 对 HKCU 和 SHCTX 均失败），Highest 计划任务同样失败；2026-10-20 最小 NSIS 探针（非 LunarShroud）完整走完向导，`.onInit` 写 `ran.txt`、安装段写 `probe.txt`/`uninstall.exe`，但未创建快捷方式/HKCU；`ran.txt` 显示 `...\Temp\Low`，开始菜单 ACL 对沙箱组只有 `ReadAndExecute`，证明是当前 Codex 会话的受限令牌，不是本产品脚本；候选安装包 `860FC8FC…` 已加显式 HKCU POSTINSTALL；2026-10-17 正常桌面曾观测到 | 再次安装会复用旧目录；删除数据/手工清理可恢复默认。发布前需在普通交互桌面再确认一次 Add/Remove 条目 |
| R9 | 非 ASCII/含空格模型路径、长路径 | 低 | 中 | 非 ASCII/空格路径实测 RUNNING、768 维嵌入；315 字符路径实测 llama.cpp 报 `failed to open GGUF`，重建后启动前检查会报 `path_too_long` | >260 字符仍不受支持，但用户会得到明确原因和“移到更短目录”的修复建议 |
| R10 | 启动器日志含每次启动的 `session_id` | 低 | 低 | 隔离日志扫描：令牌 0 命中；重建后 `backend.log` 只写 `backend started pid=…`，测试钉住不含 session | 已收口；无需再分享前手工删除 |
| R11 | 用户数据（`config.json`、`history.db`、`chats.db`、预设）无自动备份/云同步 | 低 | 中 | 配置与预设原子写入；历史/聊天库有上限；`Backup-UserData.ps1` / `Restore-UserData.ps1` 和设置页「数据备份」提供 SHA-256 清单、篡改拒绝、SQLite 快照、恢复前快照与延迟恢复；聊天附件/提示词随 `chats.db` 备份 | 仍需用户主动运行；没有云同步/自动定时备份 |
| R12 | 依赖告警（7 条）与 FastAPI/httpx 测试弃用告警 | 中 | 低 | `cargo audit` 0 漏洞；5 个 `unic-*`、`proc-macro-error`、Linux-only `glib` 告警；二者不在 Windows 依赖图 | 需跟随 Tauri/urlpattern 上游；测试告警可能在下次依赖升级时变成失败 |
| R13 | 打包态真实窗口截图/高 DPI 未在本轮完整取证 | 低 | 低 | 2026-10-21 已在真实 WebView2（debug 与全新打包对）完成截图与审计：DPR 1.545、800/DPR2 抽屉、1280/DPR2 桌面、逐页无横向溢出、console 0 warning/error；截图在 `tmp/webview2-check-20261021/`。同一轮还发现并修复打包版 PyYAML 缺失。随后 release 候选在隔离环境通过 UI Automation 实跑真实 Qwen3.5-4B：流式回答、本地附件 `8642`、上下文仪表、数据落盘和退出清理；证据在 `tmp/packaged-check-20261021-chat/` | 多显示器移动、物理 200% 缩放与不同字体仍可能影响观感；WebView2 155 的本机 CDP 端点不可用（上游回归），打包 UI 改用 UI Automation |
| R14 | 恶意/畸形 GGUF、超长请求、日志洪水等资源耗尽 | 低 | 中 | GGUF 字节/条数/嵌套预算与异常面测试；请求体、日志、历史、DOM 有上限 | 极端文件仍可能让扫描变慢；不会 500，但会跳过该文件 |
| R15 | 后端重启窗口里，前端可能把**已失效**的旧令牌发给抢到端口的进程 | 中 | 低 | 每代后端换发新 session_id+token；`get_backend_session` 在 HMAC 校验通过前返回空；前端 Tauri 模式不再回落到硬编码端口。旧令牌对新一代无效 | 严格"任何情况下都不把任何令牌发给未确认对端"仍未完全满足：`api.js` 在重连前可能用缓存 base 发一次已过期令牌。攻击者只能拿到废值 |
| R16 | 重复启动/单实例失败误报 | 低 | 低 | 2026-10-18 起持有 `Local\LunarShroud-SingleInstance` 命名互斥体；第二次启动把已有窗口 `ShowWindow(SW_RESTORE)`+`SetForegroundWindow` 后退出；mutex API 失败按 `Unavailable` 放行并记日志，不再误报"已在运行" | 互斥体是每登录会话的 `Local\`；不同 Windows 用户仍可能各起一套（端口是机器级，第二个会明确报端口冲突）。预测名称可被同用户进程抢占造成拒绝启动 |
| R17 | 监督器持续重启（30 s 封顶） | 低 | 低 | 连续快速失败按 1→30 s 退避；稳定运行 60 s 后归零；`launcher.log` 记录 uptime/failures；窗口保持可用 | 后端 exe 被删除/杀软持续拦截时仍会每 30 s 重试，没有 UI 提示；日志按 1 MiB 轮转 |
| R18 | 外链改由 `ShellExecuteW` 打开，未做真实 WebView2 点击取证 | 中 | 低 | 协议白名单（仅 http/https）有 Rust 单测；`on_new_window` 在 wry/tauri 源码层确认是唯一的拦截点 | 打包态首次真实点击未验证；若 ShellExecuteW 被策略禁用，只会记日志 |

## 2026-10-18 接管轮复核（独立核验报告）

独立核验确认 18/19 项修复在代码中有实体、部署对逐字节一致，并在部署态 onedir
上完成 9/9 实机协议验证；同时指出三项门禁问题，已处理：

- **Rust 测试不密闭**：4 个测试依赖继承的 `%TEMP%` 可写，在受限令牌下失败。
  测试目录改为放在测试二进制旁边（Cargo 刚写过、必然可写），清理失败改为
  可见告警；把 `TEMP`/`TMP` 指向不存在的路径后直接运行测试二进制，**22/22
  通过**。AGENTS §6.5 已把这条写成规则。
- **发布门禁被外部临时文件弄红**：`backend/jy4s_b4h`（4 字节，外部工具残留）
  已被清理，`debug.log` 与运行期 `backend-child.log` 一并清除；门禁对
  `backend/????????` 的 tempfile 风格残留增加窄规则，`--apply` 仍是先预检后写。
- **NVML 设备显存路径绕过哨兵**：`_nvml_query_gpus` 的 `used/total` 现在都走
  `_nvml_memory_mb`，并新增调用点测试（不只是 helper 单测）。
- **NSIS 安装/卸载已重跑（2026-10-18）**：静默安装 → 安装版启动/401/404/
  HMAC/关闭清理 → 静默卸载全部通过；新增卸载钩子清除了应用在安装目录创建的
  WebView2 缓存/日志，真实用户数据与快捷方式保持不变。卸载注册表项在本会话
  受环境限制无法观测（最小 NSIS 安装器也写不进 HKCU），需在普通交互桌面复核。
- **P3 部分 soak（2026-10-18）**：真实 Qwen3.5-4B 经启动器 API 跑 15 分钟，
  402/402 请求成功、0 失败，平均 0.242 s，RSS ~3.18 GB 全程平稳；停止后端口
  释放、无残留。完整 4–8 h soak 与多硬件矩阵仍未完成。
- 仍未重跑：多硬件矩阵；无头会话截图超时，只保留 DOM/console/network 证据。
- **4 小时 soak 已跑（2026-10-19）**：部署对在隔离
  `APPDATA`/`LOCALAPPDATA`/`USERPROFILE`/`TEMP` 中运行 Qwen3.5-4B Q4_K_M
  共 14,405 s，7,186/7,186 请求成功、0 失败，p50/p95/p99/max
  0.25/0.266/0.281/2.078 s；RSS 3,178.8→2,795.0 MB（斜率 -2.053 MB/min），
  句柄 221→271、线程 41→39，GPU 3,004 MB 稳定；实例停止、端口释放、
  启动器退出 0、无孤儿进程、真实用户数据哈希不变。日志无 traceback、
  无监督器重启。**自动化报告的唯一 FAIL 是 AC 前置检查**：12:00:07 读到
  `plugged=False`（99%），但 Kernel-Power 事件 105 显示 11:59:24 与
  12:02:40 两次电源变化，与约 3 分钟拔电窗口一致，16:03 电池 96% 且已接 AC；
  无睡眠/恢复事件，最大采样间隔 61.2 s，运行连续。产品 soak 判据通过，
  原始 `report.json`/`report.md` 保留未改，环境偏差按人工复核记录。

- **P1 NSIS 直接复跑（2026-10-20）**：`tmp/nsis_install_test.py` 对
  `4C439483…` 安装包直接运行。安装/启动/卸载/清理全部通过：`_internal`
  757 项、后端 `A1BAACAA…`、安装版 launcher `87B0B2E1…`（NSIS 变体；
  根便携版 `0E778DD0…` 仅差 3 字节 `BUNDLE_TYPE_VAR` `NSS`/`UNK`）、
  401/404/HMAC/config 200、WM_CLOSE、无后端残留、端口释放；卸载后安装
  目录（含 WebView2/log）删除，真实数据和快捷方式恢复。唯一未观测的是
  快捷方式和 Add/Remove 条目：Python HKCU 探针通过，但 2026-10-20 最小
  NSIS 探针（非 LunarShroud）完整走完向导，`.onInit` 写 `ran.txt`、安装段
  写 `probe.txt`/`uninstall.exe`，却没有创建快捷方式或 HKCU；`ran.txt` 显示
  `...\Temp\Low`，开始菜单 ACL 对沙箱组只有 `ReadAndExecute`。这是当前
  Codex 会话的受限令牌，不是本产品脚本；候选安装包 `860FC8FC…` 已加显式
  HKCU POSTINSTALL，需普通非沙箱桌面确认。官方 Tauri 重建 `0E1A0FCC…`
  的 NSIS payload 相同；`4C439483…` 已被候选 `860FC8FC…` 取代。

## 本轮实测证据

### 1. `cache_ram` 与宿主内存（R5）

同一份 Qwen3.5-4B Q4_K_M、同一台 RTX 5060 Laptop 8 GB、同一打包后端：

| 配置 | 时长/请求 | llama-server RSS | 后端 RSS | GPU | 失败 |
|---|---|---|---|---|---|
| 默认（`--cache-ram 8192`） | 610 s / 100 次 | 5.2 → 11.4 GB 后平台化 | ~64 MB | ~2.98/8 GB | 1 次（清理时中断） |
| `cache_ram: 0` | 214 s / 40 次 | 3.21 GB 稳定 | 65.2 MB | 2980/8151 MiB | 1 次（客户端首发未到达服务端） |
| `cache_ram: 0` 复测 | 10 次 | — | — | — | 0 次，10/10 HTTP 200 |

结论：默认值下 8 GB 量级的增长来自 llama.cpp 的宿主侧提示词缓存，不是启动器或
后端泄漏；设 `0` 会关闭缓存（上游同时打印 “cache-idle-slots requires cache-ram,
disabling”），RSS 回落到模型本身 + KV 的 ~3.2 GB。建议在 16 GB 以下内存的机器上
把 `--cache-ram` 调到 2048 或 0；代价是重复提示词不再复用。

### 2. 外壳监督器崩溃重启（R2 的崩溃子项）

使用隔离 `%APPDATA%`、打包版根目录对：

```text
backend ready pid=40104 port=17520
backend exited unexpectedly: Ok(ExitStatus(ExitStatus(1))); restarting
backend restarted pid=21476
```

强杀整个后端进程树后，监督器在约 2 秒内拉起了新一代后端；新 PID 监听同一
17520 端口，启动器窗口保持存活。随后正常关闭窗口：启动器退出、端口释放、
新一代后端进程消失。注意日志里的 40104 是 PyInstaller bootloader，真正监听的是
它拉起的子进程（本轮为 35536/39932），这不影响监督逻辑。

### 3. 日志中的凭据扫描（R10）

对隔离 `%APPDATA%` 下 19 个文件做 ASCII + UTF-16LE 字节扫描：

- 会话令牌 `risk-token`：**0 命中**（含 `backend.log`、实例日志、history.db、
  config.json）。
- `session_id`：仅 `backend.log` 的启动行命中；它只用于识别“这次启动的后端”，
  不是管理 API 令牌，且每次启动重新生成。

### 4. 回滚备份可用性（R3/R11 的交付子项）

`tmp/portable-backups/20261017-174210/` 保存了部署前的旧对：

```text
LunarShroud.exe   83634DCFDD83D64FF2847C9161D2976B9FEF16D56141F60B704CE133788CD592
backend.exe       E4FAE5CED7DD71B801CA16E0115BBFC1D758887D44B357E905C2296CF6840527
```

在隔离 `%APPDATA%` 中直接启动该旧对：`backend ready pid=25240 port=17520`，
窗口正常出现；正常关闭后启动器退出、端口释放。备份不是“只写了哈希”，是实际
可启动的回滚点。回滚方式是停止启动器后把这两个文件复制回原位（或重新运行
`Publish-PortableRuntime.ps1` 指向备份目录中的文件），再启动确认。

### 5. 路径、绑定与循环启停（R4/R6/R9）

- 管理后端只绑定 `127.0.0.1:18097`，没有 `0.0.0.0` 监听。
- 非 ASCII + 空格路径 `…\tmp\风险路径\模型 测试.gguf` 启动到 RUNNING，
  `/v1/embeddings` 返回 768 维向量，停止后 `exit=1`。
- 同一模型连续 10 次启动/停止：10/10 成功，10 次都复用端口 8080，结束后
  `leftover_llama=0`。

### 6. NSIS 安装/卸载原始证据（第六轮候选，历史；R8）

第六轮复核指出上一轮只有叙述、没有原始输出，因此 2026-10-17 对当时的候选
重新做了一次静默安装 → 启动验证 → 静默卸载，证据保存在维护者本地的
`tmp/risk_assessment_20261017/nsis/`（不进入发布镜像）。当前 r2 安装包的复核
见第 7 节：

| 阶段 | 结果 |
|---|---|
| 安装前 | `%LOCALAPPDATA%\LunarShroud` 不存在；无 `LunarShroud` 卸载项；无安装位置键 |
| 静默安装 | 退出码 0，耗时 2.55 s；生成 3 个文件，共 24,997,085 字节 |
| 安装产物 | `lunarshroud.exe` `076577a5…`（安装器改写，与便携候选不同）；`backend.exe` `6e94321b…`（与候选一致）；`uninstall.exe` `069d6145…` |
| 安装版启动 | 监听 17520；无令牌 `/api/system/config` → 401；错误 session 的就绪探测 → 404；正常关闭后端口释放、无残留进程 |
| 静默卸载 | 退出码 0，耗时 1.36 s；安装目录、卸载项、开始菜单/桌面快捷方式均被删除 |
| 遗留与复原 | `HKCU\Software\lunarshroud\LunarShroud` 安装位置键按设计保留；测试后已删除以恢复测试前状态；真实 `%APPDATA%\LunarShroud` 的 `config.json` / `history.db` SHA-256 前后不变 |

### 7. 第七轮加固与故障注入（2026-10-17）

- **安全/故障探针**：对重建后的部署态后端 34/34 通过——认证 401、就绪探测只认
  session、CORS/WS 来源、静态与预设路径穿越、`extra_args` 预设导入/启动拒绝、
  argv 无 shell 注入、API key 脱敏、3 MB 请求体 413、未知 API JSON 404、畸形
  GGUF 不 500、子进程环境无 `LUNARSHROUD_*`、日志无令牌。
- **损坏数据注入**：损坏 `config.json` 能备份为 `.corrupt` 并继续启动；损坏
  `history.db` 原先让后端 exit 3，现已隔离为 `history.db.corrupt` 并重建。
- **长路径**：315 字符的合法 GGUF 被 llama.cpp 拒绝打开，归因原先误报为
  “格式/下载/架构”；现由 `path_too_long` 在启动前拦下并给修复建议。
- **短时模型矩阵**：Qwen3.5-4B、Qwen3.5-9B IQ4_NL、Gemma-4-E4B、MiMo-9B、
  nomic-embed（768 维）和 Qwen3.5-4B CPU-only 全部 RUNNING 并完成一次请求；
  每次停止后无残留 llama-server。
- **并发**：8 个实例并发启动/停止，8/8 running、端口 8080–8087 唯一、8/8 嵌入
  请求成功、无残留进程。
- **备份/恢复工具**：备份→恢复 round-trip、篡改备份拒绝、清单 `../` 穿越拒绝；
  用 Windows PowerShell 5.1 实测。期间发现脚本从 PowerShell 7 继承
  `PSModulePath` 时 5.1 可能找不到 `Get-FileHash`，已改用 .NET SHA-256。
- **部署脚本**：`-Destination .` 曾按 .NET 当前目录解析而写入 `src-tauri\`；
  已改为按 PowerShell 的 `Get-Location` 解析，并有“location 与 cwd 不一致”的测试。
- **可重复构建**：固定 `SOURCE_DATE_EPOCH=1790000000` 与 `PYTHONHASHSEED=0`
  后，连续两次构建的 `backend.exe`（`30d90875…`）与 `lunarshroud.exe`
  （`67ab9f22…`）哈希完全一致。NSIS 安装包本身仍因 makensis 元数据每次不同
  （本次 `bba80682…`），因此按安装后的内层文件哈希复核，而不是假设安装包可复现。
- **NSIS（r3）**：新安装包静默安装退出 0；安装版 launcher `1cb283d2…`（安装器
  改写）、backend `30d90875…`（与新候选一致）；17520 上 401/404 正常，关闭无
  残留；静默卸载删除目录/卸载项/快捷方式，遗留安装位置键按设计保留并已清理。

## 发布判断

Beta 发布可以接受的风险：R1、R3、R4、R5、R10、R12、R13。它们要么是行业常见的
未签名 beta 状态，要么已有可执行的用户侧缓解。发布说明必须继续写明“未签名、
未完成 4–8 小时长稳、单卡验证”。

进入 1.0 前必须关闭：R2（至少 4–8 小时真实负载 + 多硬件矩阵）、R1（代码签名）、
R3（构建来源证明）。R5/R6 不阻塞 1.0，但使用指南必须继续给出 `--cache-ram` 与
`--api-key` 的建议。

## 未验证清单（不要当作已通过）

- 4–8 小时连续真实对话/嵌入负载；长时间 GPU 温度/功耗墙下的行为。
- AMD、Intel、纯 CPU、多 GPU、不同 CUDA/Vulkan 构建的兼容性。
- Windows 10 实机（本轮为 Windows 11）、Windows PowerShell 5.1 之外的安装组合。
- 真实 WebView2 在多显示器/高 DPI 下的截图级验收。
- 断网、磁盘满、只读 `%APPDATA%`、杀毒软件隔离后端等故障注入。
- 代码签名后的 SmartScreen 行为与自动更新链路（当前都没有实现）。
