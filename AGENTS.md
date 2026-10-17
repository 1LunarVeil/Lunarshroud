# AGENTS.md — 动手改这个项目之前

本文件是给协作者（人或编码代理）的工程规约，只写**从代码里看不出来**的东西：
某个写法为什么是错的、哪条不变量一旦被破坏就会把已修过的缺陷重新引入。

> 裁判是测试与门禁，不是文档。包括本文件，也包括 `docs/SECURITY_MODEL.md`——
> 那份文档描述的是**意图**，在它的每条断言被测试钉住之前，请当作未验证。
> 本项目此前的三个 P1 安全问题，全都属于"文档写了、代码没做"。

## 1. 改完必须跑的三条命令

```bash
python -m pytest backend/tests -q          # 全量后端测试
python -m ruff check backend tools         # lint
python tools/check_release_mirror.py       # 发布镜像门禁（见 §3）
```

前端另有 `node --test frontend/tests/*.test.mjs`；Rust 侧 `cargo test`（在 `src-tauri/`）。

**"CI 绿"不等于"能跑"。** CI 的 PowerShell 步骤用 `shell: pwsh`（PowerShell 7，
强制 UTF-8），而用户机器上默认是 Windows PowerShell 5.1。已有真实教训：一个
`.ps1` 在 5.1 上直接跑不起来，CI 全程无感（见 §2.8）。凡是**用户会手动执行**的
脚本，必须用 `powershell.exe` 实测过，不能只用 `pwsh` 验证。

## 2. 不变量清单

每条格式：**规则 → 为什么 → 已被哪个测试钉住**。破坏前两条不会立刻报错，
破坏第三条会。

### 2.1 绝不用裸名解析可执行文件

`llama_server_path` 的默认值必须是**空字符串**。任何非空默认值都会让一个从未被
用户选择过的裸文件名进入解析流程，而解析的第一站是应用目录——打包态下那就是
**用户可写的安装目录**，于是每次启动、零交互地执行一个别人能写入的路径。

同理：所有系统工具（`taskkill.exe`、`nvidia-smi.exe`）必须走
`server_probe.system_tool()` 的 System32 绝对路径；`PATH` 查找必须跳过空条目
（空条目 = 当前目录）。找不到时**禁用该功能**，绝不退回裸名 ——
`nvidia-smi` 现在返回 `None` 并跳过 GPU 指标，而不是按搜索序执行应用目录里的
同名文件（`test_regressions.py::test_nvidia_smi_is_resolved_to_an_absolute_path_or_disabled`）。

*为什么*：Windows 的 exe 查找顺序里 CWD 与应用目录都优先于 System32，
这是 Linux 安全直觉最容易翻车的地方。
*钉它的测试*：`test_regressions.py` 里的 CWD 植入件拒绝、空默认值行为、
`Popen` 确实收到 `cwd=` 与过滤后的 `env=`。

### 2.2 会话令牌不得发给"尚未确认是谁"的对端

就绪探测走 `/api/system/ready?session=<session_id>&nonce=<nonce>`：它**不要
令牌**；后端回答 `proof = HMAC-SHA256(token, "lunarshroud-ready-v1:" + nonce)`。
外壳必须校验 `proof`，在通过之前不得把 `X-LunarShroud-Token` 交给前端或任何
对端。

**`session_id` 不能当身份证明**：它就在外壳发出的请求 URL 里，任何收到请求的
本地进程都能原样回显。早期实现正是用"后端回显 session_id"来认人，于是一个抢先
绑定端口的进程只要回显就会通过，随后拿到 WebView 发来的令牌。只有持有令牌的
后端才能对**新的** nonce 算出正确摘要。

同理，每一代后端重启都必须换发新的 `session_id` 与令牌，且
`get_backend_session` 在新一代通过 HMAC 校验前必须返回空；Tauri 模式下的前端
不得在拿不到已验证会话时回落到硬编码端口 —— 那正是"把令牌送给占端口的人"。

*为什么*：Windows 回环端口**不按用户隔离**，任意本地进程都能占住该端口；
拿到令牌即可 `PUT /api/system/config` 改 `llama_server_path` → 以受害者身份
执行任意程序。
*钉它的测试*：`test_auth_coverage.py`（路由逐个扫：无令牌必须 401、有令牌必须
非 401，豁免名单是冻结集合）、`test_regressions.py` 的就绪 HMAC/非 ASCII 输入
用例、`src-tauri/src/lib.rs` 的 `readiness_proof_is_hmac_sha256_over_context_and_nonce`、
`forged_peer_echoing_the_session_id_is_rejected`、
`session_is_not_exposed_until_a_generation_is_verified` 与
`readiness_probe_carries_a_nonce_but_never_the_token`。

### 2.3 `extra_args` 是能力，不是越权入口

受管参数 `host` / `api_key` / `port` / `path` / `log_file` **不得**被
`extra_args` 覆盖，且在**导入预设时**就要拒绝（不能只在启动时拒）。

*为什么*：一条 `--host 0.0.0.0` 就能击穿代码自己声明的安全边界
（非回环地址必须配 `api_key`）。
*钉它的测试*：`param_builder.find_forbidden_extra_args`、`preset_store` 导入校验。

### 2.4 子进程环境必须剥掉 `LUNARSHROUD_*`

`server_probe.sanitized_child_env()` 大小写无关地清除全部 `LUNARSHROUD_*`
变量；`process_manager._child_env()` 委托给它。**任何**会启动子进程的路径都要
用它：llama-server 启动、能力探测（`--help` / `--version`）、`nvidia-smi`
采样、`taskkill`。

*为什么*：`llama-server` 是第三方二进制、探测目标还是用户可配置的任意路径，
它们都不该拿到启动器的会话凭据——一次任意文件执行就能从"当前进程"升级为
"跨启动持久化"。探测路径曾漏传 `env=`，启动路径剥了、探测路径没剥，等于白剥。
*钉它的测试*：`test_regressions.py` 的 `Popen`/`_child_env` 用例、
`test_server_probe.py::test_probe_child_process_never_inherits_the_session_token`。

### 2.5 脱敏是**内容级**的，不是按键名的

`scrub_sensitive_params` 对任何包含 `--api-key` 的字符串值做整段清洗。
只按参数名脱敏会漏：密钥藏在 `extra_args` 里就会被明文写进 `history.db`
和 `presets/*.json`，并能经 export 附件外泄。

### 2.6 GGUF 解析必须有预算

字节预算 + KV 条数上限 + **数组嵌套深度上限**，且异常面必须覆盖
`RecursionError` / `struct.error` / `OSError`。一个畸形文件不得让任何接口 500，
更不得拖死扫描循环——解析失败返回 `None`，调用方跳过该文件。

### 2.7 WebSocket / CORS 的可信来源必须随 `launcher_port` 派生

`main.trusted_origins(port)` 由生效端口构造；`launcher_port` 是用户可改的。

*为什么*：把默认端口写死进信任表，改过端口的人在源码运行通道里**每个 WebSocket
都被 403 拒掉**，而 HTTP 同源不受影响——界面看着完全正常，只有实时推送死了。
*钉它的测试*：`test_regressions.py` 的 `test_ws_trust_follows_the_configured_port_not_the_default`
（走真实调用链，不走纯函数——只测纯函数的话，把端口写死在调用点上照样全绿）。

### 2.8 PowerShell 脚本的两条硬规则

- 含非 ASCII 注释的 `.ps1` **必须带 UTF-8 BOM**。5.1 没有 BOM 就按机器 ANSI
  代码页（中文 = GBK）解码，注释会**吞掉紧随其后的代码行**且不报语法错。
- `param()` 的默认值里**不得用 `$PSScriptRoot`**（5.1 求值默认值时它是空的）。
  要默认路径就进函数体再解析。
- 用户脚本**不得依赖可能受继承 `PSModulePath` 影响的 cmdlet**（典型：
  `Get-FileHash`）。从 PowerShell 7 启动 `powershell.exe` 时，5.1 会继承 7 的
  模块路径，`Microsoft.PowerShell.Utility` 可能不自动加载，脚本在用户机上直接
  报 `CommandNotFoundException`；而手工从别的 shell 运行又可能看不到。哈希用
  .NET `System.Security.Cryptography.SHA256` 计算（见 `tools/Backup-UserData.ps1`）。

*钉它的测试*：`test_portable_runtime.py::test_non_ascii_ps1_scripts_declare_utf8`、
`test_portable_runtime.py::test_user_data_backup_and_restore_round_trip`
（后者经 pytest 继承的 PowerShell 7 环境启动 5.1，能复现模块路径问题）。

### 2.9 实例重启按**链根**串行

`restart_instance` 必须先锁 `chain_root` 那一代，再沿 `replaced_by` 往下锁；
对着已被替换的旧 ID 重启应当是**空操作**（返回当前存活的那一代）；
停止失败（`FAILED`）时**必须拒绝**启动新一代。

*为什么*：只锁"被点名的那一代"是不够的——从下游 ID 进来的重启不碰上游锁，
结果是两条实例记录同时 `running`、同时占同一个端口、两个服务进程并存。
而"停止失败就别再启"这条不变量不能靠端口实测兜底：那是个连带的、
报"端口被占用"的巧合，自动分配端口时根本不成立。
*钉它的测试*：`test_e2e_smoke.py` 的三条重启并发测试（真子进程）。

### 2.10 回环探测不得经过代理

后端实例健康探测（`ProcessManager._probe_health`）必须带 `trust_env=False`；
外壳的就绪探测（`wait_backend_ready`）必须 `.no_proxy()`。

*为什么*：`httpx` 默认读取 `HTTP_PROXY`/`HTTPS_PROXY`，在 Windows 上还会回退到
注册表里的系统代理。用户开着 Clash/V2Ray 但代理尚未启动时，发往
`127.0.0.1:<port>/health` 的请求会被送进死代理并超时 —— llama-server 明明已就绪，
界面却报 "startup timed out"。回环流量不属于任何代理的管辖范围。
*钉它的测试*：`test_regressions.py::test_health_probe_never_uses_an_environment_proxy`
（把代理指向一个没人监听的端口，断言探测仍成功）。
Rust 侧的 `.no_proxy()` 没有可离线单测的接缝，2026-10-17 用死代理环境启动真实
打包对做了端到端验证。

### 2.11 启动时必须收口上一进程的未完成历史

`lifespan` 必须调用 `history_store.mark_unfinished_as_interrupted()`，把
`starting` / `running` / `stopping` 的记录改成 `interrupted`；`stopped_at` 与
`duration_seconds` 保持 NULL（真实停止时间未知，不能编）。

*为什么*：外壳关闭窗口时通过 Job Object 直接终止后端，FastAPI 的 lifespan
shutdown 钩子**不会执行**。没有收口的话，历史页会永远把早已不存在的实例显示成
"运行中"（实测发布库里躺着数周前的 `running` 记录）。
*钉它的测试*：`test_reliability_features.py` 的
`test_unfinished_history_rows_are_marked_interrupted` 与
`test_backend_startup_reconciles_unfinished_history`（后者走真实 lifespan，
防止只测纯函数）。

### 2.12 损坏的启动数据必须隔离重建，不能拖死启动

`config.json` 解析失败要备份为 `config.json.corrupt` 后用默认值继续；
`history.db` 被 SQLite 判定为损坏（`file is not a database` / `database disk
image is malformed`）时，必须把主库和 `-wal`/`-shm` 一起改名为
`history.db.corrupt*`，再建新库。锁冲突、权限错误不得误判为损坏，仍要原样
抛出。

*为什么*：2026-10-17 故障注入把非 SQLite 文件放进 `history.db`，打包后端在
lifespan 的 `init_db()` 抛 `DatabaseError`，进程以 exit 3 退出，用户只看到
"后端启动失败"；一个可选的历史库不该让整个启动器不可用。
*钉它的测试*：`test_reliability_features.py::test_corrupt_history_database_is_quarantined_and_rebuilt`
与非 SQLite 文件的版本并列的
`::test_data_page_corruption_is_quarantined_not_just_non_sqlite_files`
（合法头 + 坏数据页 —— 只建表不读页时它会漏过，启动后 history 才 500）。

### 2.13 部署脚本的相对路径按 PowerShell 位置解析

`Publish-PortableRuntime.ps1` 的 `-Destination` / `-BackupDirectory` 若是相对
路径，必须用 `Get-Location` 拼接后再 `GetFullPath`，不能只调用
`[IO.Path]::GetFullPath($relative)`。

*为什么*：PowerShell 的 `Set-Location` 与 .NET 的 current directory 可能不一致。
2026-10-17 实测 `-Destination .` 把新对部署进了 `src-tauri\`（并把桌面快捷方式
指过去），而工作区根目录仍是旧对。
*钉它的测试*：`test_portable_runtime.py::test_deployer_resolves_relative_destination_from_powershell_location`
（故意让 PowerShell location 与进程 cwd 不同）。

### 2.14 停止/终结路径的三条铁律

1. **stdout 管道只能由读它的那个线程关闭**。读线程阻塞在 `for line in stream`
   时握着 IO 锁；任何其他线程（停止路径、监控线程的 finalize）调 `close()`
   都会与它互等，把停止请求永久挂死。
2. **`taskkill` 返回非 0 不得当作"已杀掉"**。它会被安全软件/组策略拦截；
   必须落到 psutil 树杀（按父子关系，不依赖 taskkill 的权限模型），再退回
   直接 `kill()`。
3. **终止失败必须保留进程句柄与端口预留**，并允许再次 stop。把 `process`
   置 `None` 等于丢掉唯一能杀它的句柄；提前 `_release_port` 会让端口被分配给
   下一个实例。`remove_instance` 在仍有活进程时必须拒绝。

*为什么*：以上三条任一被破坏，表现都不是报错而是"停止按钮没反应 / 历史里
一直是 FAILED / 端口占着却没人能停"，而且只在 taskkill 被拦或进程受保护时
才出现 —— CI 默认环境里看不到。
*钉它的测试*：`test_regressions.py::test_finalize_never_closes_a_pipe_owned_by_the_reader_thread`、
`::test_taskkill_nonzero_exit_falls_back_and_kills_the_whole_tree`、
`::test_failed_termination_keeps_the_live_handle_and_reserved_port`。

### 2.15 后端是 onedir，部署必须整目录

`backend/dist/backend/` 里的 `backend.exe` 与 `_internal/` 是一个不可拆分的
运行时。`src-tauri/build.rs`、`tauri.conf.json` 的 bundle resources、
`Publish-PortableRuntime.ps1` 都必须整棵复制并逐文件校验；只复制 exe 会得到
一个启动即失败的 sidecar（找不到 Python 运行时），而用户只会看到"后端启动失败"。

*为什么*：onefile 每次启动要解压 ~20 MB 到 `%TEMP%`，在用户的临时目录被
重定向/只读/安全软件拦截时，PyInstaller 只弹 "Could not create temporary
directory!" 然后挂住，启动器拿不到可读原因；强杀还会在 Temp 里留下解压目录
（实测 167 个、2.2 GB）。onedir 不走临时目录，在 `TEMP` 指向不存在路径时仍能启动。
*钉它的测试*：`test_portable_runtime.py::test_portable_runtime_deployer_copies_the_onedir_runtime`
（真实 PowerShell 5.1、含子目录与逐文件哈希）；`build.rs` 在 release 缺目录时
直接 panic；安装包的 NSIS 脚本包含 `backend\_internal` 文件项。

### 2.16 数据/日志目录必须"可写才用"

打包态的 `DATA_DIR` 不能直接信任 `%APPDATA%`：启动器可能被沙箱/受限令牌拉起
（实测 `%APPDATA%\LunarShroud\logs` 上只有 `CodexSandboxUsers:(RX)`），此时
`configure_file_logging`/历史库会抛异常，uvicorn 以 **exit 3** 退出，而用户和
启动器都看不到日志。正确行为：按 `%APPDATA%` → `%LOCALAPPDATA%` →
`%USERPROFILE%` → 应用目录顺序探测**实际可写**的目录；日志文件打不开只能降级，
不能让 lifespan 失败。启动器侧同样要回退日志/临时目录，并在 exe 旁写
`launcher-boot.log`、把后端 stderr 落到 `backend/backend-child.log`。

*钉它的测试*：`test_regressions.py::test_frozen_data_dir_falls_back_when_appdata_is_unwritable`；
打包态探针把 APPDATA/LOCALAPPDATA/USERPROFILE/TEMP/TMP 全部指向一个文件仍能
`backend ready`。

### 2.17 单实例：第二次双击不得再起一套后端

启动器必须持有会话级命名互斥体（`Local\LunarShroud-SingleInstance`）；检测到
已有实例时，优先 `ShowWindow(SW_RESTORE)`+`SetForegroundWindow` 把已有窗口拉到
前台并退出，找不到窗口才弹"已在运行"提示。创建窗口前后都要写
`launcher-boot.log`（`creating`/`created`/`hwnd visible_before/after`），窗口构建
报错要显式弹窗，不能把进程留在事件循环里变成"看不见的僵尸"。

*为什么*：用户连续双击两次时，第二个后端抢不到 17520，uvicorn 以 **exit 3**
退出，启动器弹"后端进程启动失败"；而第一个实例其实窗口已经建好，只是没有前台
显示。用户只会看到那个误导性的错误弹窗（实测 2026-10-18：第一次 13:45:48
`backend ready`，第二次 13:46:14 `bind 10048`）。
*钉它的测试*：`src-tauri/src/lib.rs` 的 `acquire_single_instance`（含 LastError
清零，避免误判）；打包对实测第二次启动只剩一套 `LunarShroud.exe`/`backend.exe`，
`launcher-boot.log` 记录 `setup: main window created`。

### 2.18 WebView2 数据目录必须显式放可写位置

`WebviewWindowBuilder` 必须 `.data_directory(<exe_dir>/webview2-data)`。不能依赖
Tauri 默认的 `%LOCALAPPDATA%\<identifier>`：受限令牌/沙箱下该目录创建会被拒绝，
`CreateCoreWebView2Environment` 返回 `os error 5`，窗口建不出来，进程却留在事件
循环里——用户看到的是"双击没反应"，再点一次就变成 17520 端口冲突的
"exit code: 3" 弹窗。`launcher-boot.log` 的 `setup: main window created` +
`hwnd=... visible_before/after` 是这条路径的验收证据。

*钉它的测试*：`/RL LIMITED` 计划任务启动打包对，boot log 记录
`setup: main window created`、`visible_before=true/visible_after=true`，后端日志
出现 `WebSocket /ws/instances [accepted]`。

## 3. 发布镜像与门禁

`github/` 是**精选的公开镜像**，不是工作树快照。任何被镜像文件的改动，
改完必须：

```bash
python tools/check_release_mirror.py --apply   # 同步
python tools/check_release_mirror.py           # 复查，必须 CLEAN
```

门禁是 **fail-closed** 的：读不了的文件、超大的文件一律按命中处理。
不要为了通过而放宽规则，要为了通过而**修内容**。隐私扫描覆盖
UTF-8 / UTF-16LE / UTF-16BE / latin-1 以及 gzip/zlib 容器——所以"把路径写进
二进制或压缩文件"也不是出路。

**刻意不发布**（在 `CURATED_EXCLUDES` 里有规则，不是靠记忆）：
内部审计文档、AI 编码工具的配置与忽略文件、`data/`、`tmp/`、一切构建产物。

## 4. 产品边界（不要重新提议）

- **不打包、不自动下载 `llama-server`。** 每人环境不同，llama.cpp 自己按环境
  发布了预构建版本；默认路径留空 + README 指引获取方式，是刻意的。
- **双通道是硬要求**：既能 clone 源码自己编译，也能下载装好即用的安装包。
  只在一条通道上工作的改动算缺陷。
- 参数界面**跟随本机那份构建的能力**（`server_probe` + `capabilities`），
  不要把 llama.cpp 的 flag 列表当常量抄进代码。

## 5. 前端约定

- 新页面的空态一律用 `panel_views.emptyStateHtml`（它区分"目录未配置"与
  "无匹配"两种引导）。
- 页面若在异步数据到达后才具备面板内容，**必须**在数据就绪后调用
  `appCtx.refreshPanel()`，否则面板停在"加载中"。
- 后端错误消息一律经 `i18n.localizeBackendError` 呈现；新增后端错误文案时
  **必须**同时补 `ERROR_PATTERNS` 与中英两份字典（有 key 奇偶性测试兜底）。
- 令牌只经 Tauri IPC 获取（`get_backend_session`），不得出现在前端源码或 URL 里。

## 6. 验证纪律（本项目按这条验收）

1. 声称"修好了"之前，必须给出**跑过的命令与它的输出**，不是推理。
2. 回归测试要做**红-绿**：把修复退回错误版本，测试必须变红；
   只"写完就绿"的测试可能什么都没测（本项目出现过两次）。
3. UI 改动要真开浏览器看渲染，console 里的报错算失败；纯接口测试测不到
   同源/跨源差异、测不到 WebSocket 握手、测不到 PowerShell 版本差异。
4. 测试不得写真实 `data/`。`backend/conftest.py` 在任何测试模块导入 `config`
   之前把 `DATA_DIR` / `HISTORY_DB` / `PRESETS_DIR` 重定向到会话级临时目录；
   进入 lifespan 的测试会建库、清洗密钥并收口历史状态，一旦漏掉就会改写用户
   数据（2026-10-17 实测发生过）。`test_regressions.py::test_test_suite_never_uses_the_repository_data_directory`
   钉住这一性质。
