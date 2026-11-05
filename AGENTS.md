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

补充（2026-10-18）：`system_tool()` 取不到系统目录时返回 `None`，调用方必须
**禁用**该工具；Rust 外壳用 `GetSystemDirectoryW` 而不是 `SystemRoot` 环境变量。
`which_on_path()` 跳过相对 `PATH` 项；`probe_server()` 只按应用目录/PATH 解析
相对名，解析失败返回"未探测"，绝不相对 CWD 执行。
钉它的测试：Python 的 `test_system_tools_are_disabled_when_the_system_dir_is_unavailable`、
`test_which_on_path_ignores_relative_path_entries`、
`test_probe_server_never_executes_a_relative_name_from_cwd`；Rust Windows 侧的
`system_tools_are_resolved_inside_system32` 与
`missing_system_tools_are_disabled_instead_of_falling_back_to_a_bare_name`
（`system_tools_are_disabled_without_systemroot` 在 Rust 里是 `cfg(not(windows))`，
只验证非 Windows 的编译期分支，不能当 Windows 证据）。

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
停止失败（`FAILED`）且**进程句柄仍存活**时**必须拒绝**启动新一代；已经退出的 FAILED 实例允许重启。

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
4. **端口预留按所有者释放**。重启会复用同一个 `(host, port)`；旧一代的监控
   线程/终态剪枝不得删除新一代的预留。只有确认没有其他
   `starting`/`running`/`stopping` 实例拥有该键、且本进程已死亡时才
   `discard`；监控线程在 stop 期间被唤醒但进程未死时，不得伪造 `STOPPED` 或
   释放端口。钉它的测试：`test_port_reservation_survives_the_old_generations_stale_release`、
   `test_release_port_keeps_reservation_while_the_process_is_alive`。

*为什么*：以上三条任一被破坏，表现都不是报错而是"停止按钮没反应 / 历史里
一直是 FAILED / 端口占着却没人能停"，而且只在 taskkill 被拦或进程受保护时
才出现 —— CI 默认环境里看不到。
*钉它的测试*：`test_regressions.py::test_finalize_never_closes_a_pipe_owned_by_the_reader_thread`、
`::test_taskkill_nonzero_exit_falls_back_and_kills_the_whole_tree`、
`::test_failed_termination_keeps_the_live_handle_and_reserved_port`。

### 2.15 后端是 onedir，部署必须整目录


`backend/dist/backend/` 里的 `backend.exe` 与 `_internal/` 是一个不可拆分的
运行时。`src-tauri/build.rs` 与 `Publish-PortableRuntime.ps1` 都必须整棵复制并
逐文件校验；只复制 exe 会得到一个启动即失败的 sidecar（找不到 Python 运行时），
而用户只会看到"后端启动失败"。部署脚本还必须**拒绝**缺少 `_internal/` 的构建，
并在换入/校验失败时恢复旧的 launcher/backend/runtime 三段（不能留下"新 exe +
旧运行时"的混合对）。

**安装包通道已于 2026-11-05 移除**（见 §4），因此 `src-tauri/nsis/hooks.nsh` 与
`tauri.conf.json` 的 bundle resources 都不再存在，`build.rs` 也不再往
`src-tauri/backend/` 做 staging 副本。`bundle.active` 为 `false`，发布工作流
用 `--no-bundle` 构建设计产物：任何重新引入安装包的改动都必须同时恢复卸载钩子、
沙箱目录权限与安装/卸载往返验收，否则就是在分销一条没人验收的链路。

*为什么*：onefile 每次启动要解压 ~20 MB 到 `%TEMP%`，在用户的临时目录被
重定向/只读/安全软件拦截时，PyInstaller 只弹 "Could not create temporary
directory!" 然后挂住，启动器拿不到可读原因；强杀还会在 Temp 里留下解压目录
（实测 167 个、2.2 GB）。onedir 不走临时目录，在 `TEMP` 指向不存在路径时仍能启动。
*钉它的测试*：`test_portable_runtime.py::test_portable_runtime_deployer_copies_the_onedir_runtime`
（真实 PowerShell 5.1、含子目录与逐文件哈希）；`build.rs` 在 release 缺目录时
直接 panic；`test_packaging_contracts.py::test_build_rs_does_not_stage_a_bundle_resource_copy`
钉住 staging 副本不会被重新引入。

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

### 2.19 配额已满时不得静默删除用户内容

聊天、单会话消息、提示词库达到固定上限时，必须返回可读的 4xx 并保持旧数据不变；
允许“最后一条 assistant 回复”完成当前 user 轮次，但不能为了腾位删除旧会话/旧消息。
前端必须把这条错误经 `localizeBackendError` 呈现。

*为什么*：静默 LRU 淘汰会在用户没有删除操作、也没有提示的情况下永久丢失聊天记录。
*钉它的测试*：`test_chat_store.py` 的 `test_message_cap_refuses...`、
`test_conversation_cap_refuses...`、`test_assistant_can_complete...`、
`test_branching_at_the_conversation_cap_refuses...`。

### 2.20 异步弹窗动作必须等待结果

`showModal` 的动作处理必须 `await` 异步 `onClick`；失败返回 `false` 时保持弹窗打开，
成功提示只能在 API 成功后出现。聊天设置不得在请求前提交本地状态。

*为什么*：旧实现把 Promise 当成成功并立即关闭，保存失败时用户既看不到错误、也丢失
输入；聊天设置还会在失败后继续提示“已保存”。
*钉它的测试*：`frontend/tests/runtime.test.mjs` 的 `modal actions await...`、
`chat settings` 失败的浏览器验收（内部整改记录，不发布）。

### 2.21 日志缓冲同时受行数与字节预算约束

每个实例的内存日志除 `log_buffer_lines` 外还必须有字节上限
（`MAX_LOG_BUFFER_BYTES`）；单行截断不能替代总字节预算。

*为什么*：`100000` 行 × `16 KiB` ≈ 1.6 GiB/实例；第三方构建的异常输出足以拖垮启动器。
*钉它的测试*：`test_reliability_features.py::test_instance_log_buffer_respects_a_byte_budget`。

### 2.22 异步页面加载必须带请求代次

会话详情、预设详情、历史详情、Dashboard 异步订阅、参数命令预览等“最后响应获胜”
的路径必须使用 `createRequestGate`（或等价 token）；离开页面/切换目标后不得再用旧
响应写状态。

*为什么*：慢响应覆盖新选择会让消息写入错误会话、对错误预设执行删除，或让命令预览
与实际启动参数不一致。
*钉它的测试*：`frontend/tests/runtime.test.mjs` 的 `createRequestGate...`；浏览器验收
覆盖了聊天/预设快速切换和设置保存失败。

### 2.23 知识服务必须走 ProcessManager，并按阶段卸载

`knowledge_services` 启动 embedding/rerank/OCR 时必须调用现有
`ProcessManager.start_instance`，不得直接 `Popen` 一个模型服务；索引阶段先完成
OCR 提取并释放 OCR，再启动 embedding。

*为什么*：直接起进程会绕过绝对路径解析、端口预留、环境清洗、日志收口和 Job
Object；OCR 与 embedding 同时常驻还会让 8 GB 显存的设备在索引中直接失败。
*钉它的测试*：`test_knowledge_services.py` 的复用/卸载测试，
`test_knowledge_indexer.py::test_scanned_pdf_uses_ocr_once_and_caches_pages`
断言第二次强制重建不再获取 OCR。

### 2.24 知识库配额与损坏必须可见，不得静默丢数据

每个库的 2,000 文件、512 MiB 原始资料、20,000 片段上限在**写入前**拒绝；
OCR/嵌入失败只标错当前文档并保留旧索引。损坏的 `knowledge.db` 按 SQLite 恢复
流程隔离重建，不能拖死启动器。

*为什么*：知识库是用户资料资产；一次超限或半截替换会制造不可恢复的内容缺失。
*钉它的测试*：`test_knowledge_indexer.py::test_chunk_cap_errors_without_deleting_previous_chunks`、
`test_knowledge_store.py`、`test_knowledge_api.py`。

### 2.25 Chat 引用是响应契约，不是前端拼装

后端在转发前检索并把来源以 base64url JSON 写入
`X-LunarShroud-Sources`；前端解码后必须持久化到 assistant `stats.sources`，
Markdown 导出同样保留。检索失败不得回退成“无引用正常回答”。

*为什么*：前端若自己拼引用，历史重载、导出和不同客户端会各说各话；失败静默
降级则让用户误以为回答有资料支撑。
*钉它的测试*：`test_chat_api.py::test_chat_proxy_injects_knowledge_and_returns_citations`、
`frontend/tests/chat.test.mjs` 的引用导出测试。

### 2.26 浏览器验收不得靠内联脚本绕过 CSP

Playwright 测试注入 axe 必须经同域路由加载脚本，不能 `addScriptTag({content})`；
后端在测试中用 `LUNARSHROUD_ALLOW_INSECURE_DEV=1` 启动，测试结束必须停止进程。

*为什么*：应用 CSP 是 `script-src 'self'`，内联注入会让“测试通过”依赖关掉真实
安全策略；后台 dev server 泄漏则会让后续端口检查假绿。
*钉它的测试*：`frontend/tests/browser/core-paths.test.mjs`；CI 的
“Browser core-path, accessibility and performance gate”步骤。

### 2.27 被提升到共享工具栏的控件不得依赖 root 委托

`workspace.js` / `observability.js` 会把子模块的 `.page-actions` **整块搬进**共享
工具栏。搬走之后这些节点就**不在子模块 `root` 的子树里**，于是：

- 挂在 `root` 上的委托监听器永远收不到它们的点击；
- `root.querySelectorAll(...)` 也再也找不到它们（同步 active 状态会静默失效）。

控件要活着，必须**握住节点本身**：监听器绑在控件容器上（节点移动时监听器跟着
走），状态同步用 init 时捕获的节点引用，不要用 `root` 现查。

*为什么*：实测踩过——“卡片/列表”切换开关点下去**完全无反应**，而原有的
`core-paths` 浏览器测试只断言 `.segmented-btn.active` 的文本是“卡片”，于是它
**因为开关是死的才一直绿**。同类问题还有 `models.js` 的委托顺序：
`[data-quick-launch]` 嵌在带 `data-open-model` 的 `<article>` 里，先匹配卡片分支
就会 `return`，所以“快速启动”按钮其实从未被点到过。更具体的动作必须排在前。
*钉它的测试*：`frontend/tests/browser/deep-paths.test.mjs` 的
`every non-destructive control on a tabbed page actually does something`
（逐个点击，要求 DOM 变更/请求/弹窗/toast 至少发生一个）与
`model cards: detail, view toggle and quick-launch preflight are real interactions`
（断言行数与卡片数相等、断言取消预检后没有新建实例）。

### 2.28 全文检索的查询必须拆成子句，不能整句加引号

`knowledge_store.search_fts()` 不得把用户整句包成一个 FTS5 短语。
`chunks_fts` 用 `tokenize='trigram'`，整句短语等价于“必须存在逐字相同的子串”，
对自然语言问句几乎永远为空——混合检索会**静默退化成纯向量**。
中文问句必须拆成 CJK 连续段再取 3-gram 子句用 `OR` 组合（trigram 至少 3 字）。

*为什么*：实测量化过——在 377 片段法律法规库、17 道标注题上，整句加引号时
`hybrid` 与 `dense` 的排序**一模一样**（17 题中lexical 分支命中 0 次，
hit@1 35%、MRR 0.432）。拆成子句后：hybrid hit@1 **52%**、MRR **0.634**；
hybrid+rerank hit@1 **76%**、hit@3 **94%**、MRR **0.843**。
已有的 `test_fts_search_handles_chinese_and_escapes` 只测了“密码法”这种三字
精确短语，所以它**恰好测的是退化路径也能过的特例**，什么都没钉住。
*钉它的测试*：`backend/tests/test_knowledge_store.py::
test_fts_search_matches_a_paraphrased_question`（改回整句加引号必红）。

### 2.29 用户点名给进来的缺失路径必须报错，不得静默跳过

`knowledge_extract.collect_paths()` 对**用户显式列出**的不存在路径必须抛
`ValueError`；只有目录遍历中消失的文件、符号链接与 reparse point 才继续跳过。

*为什么*：“添加资料”成功提示是按这条调用的成功来发的；静默跳过会让界面显示
已添加，而那份资料其实从未入库，用户不会再去查。这是 §2.24 的同一条不变量在
“选择”而不是“配额”上的形式。
*钉它的测试*：`test_knowledge_extract.py::
test_collect_paths_refuses_a_named_path_that_does_not_exist`、
`frontend/tests/browser/adversarial.test.mjs` 的第一条（要求 400 + 本地化 toast
+ 弹窗保持打开且输入不丢）。

### 2.30 知识服务必须用 lease()，且复用不得夺走回收权

两条相关约束：

1. 索引阶段取嵌入服务必须走 `knowledge_services.lease(...)`（上下文管理器），
   不能用裸 `acquire()`。
2. `_reuse_running()` 返回的句柄，**只要那台实例的名字带 `[KB] ` 前缀就必须仍然
   `owned=True`**。

*为什么*：`release()` 对 `owned=False` 的句柄直接返回。曾经的行为是：索引一旦
复用了一台正在跑的 `[KB]` 实例，那台实例就永久躲过空闲卸载 —— 实测有一个嵌入
服务在文档承诺的 10 分钟之后仍然常驻 **22 分钟以上、约 2 GB 显存**。同时索引
阶段用的是裸 `acquire()`，没有任何人负责释放，而 reaper 只在 `lease()` 里启动，
所以"跑完一次索引"本身就等于"服务常驻"。这条不变量是 8 GB 单卡能反复索引的前提。
*钉它的测试*：`test_knowledge_services.py::
test_a_reused_knowledge_service_is_still_reaped`、
`test_knowledge_indexer.py::test_indexing_releases_the_embedding_lease`

### 2.31 前端读取的每个自定义响应头都必须进 CORS expose 清单

打包态页面源是 `tauri.localhost`，对后端 API（`http://127.0.0.1:<port>`）是
**跨源** fetch：浏览器只允许 JS 读 CORS 安全列表头 +
`Access-Control-Expose-Headers` 清单里的头。`main.py` 的 expose 清单必须覆盖
前端 `headers.get('X-LunarShroud-*')` 实际读取的**全部**头；前端新增读取任何
自定义响应头时，必须同步把它加进清单。

*为什么*：2026-10-25 独立审计实测——清单只含 `X-LunarShroud-Dropped-Turns`
时，知识库引用（`X-LunarShroud-Sources`）与未命中提示（`X-LunarShroud-Knowledge`）
在**安装版**里永远读到 `null`：引用不显示、不落库、导出缺 Sources 段、未命中
不提示，而后端其实把资料注入了模型（回答里带着 `[1][2]` 编号）。源码运行通道
（同源）与 TestClient（无 CORS 过滤）都测不出这条断裂——三道现有门禁全是同源的。
*钉它的测试*：`test_auth_coverage.py::
test_cross_origin_responses_expose_every_chat_contract_header`（带真实 Origin
走完整中间件链）、`test_frontend_contracts.py::
test_every_custom_response_header_the_frontend_reads_is_cors_exposed`
（从前端源码反向扫描读点、与真实中间件配置对账，防止清单与前端再脱节）。

### 2.32 知识服务的回收与租约必须按服务键互斥

`knowledge_services` 的 `acquire`/`lease`/`release` 对**同一个服务键**的
操作必须经 `_key_lock_for(key)` 串行化：`lease` 在键锁内完成"acquire + 标记
`_ACTIVE`"；`release` 在键锁内复核 `_ACTIVE` 并把实例加入 `_RETIRED` 后才能
stop；`_reuse_running` 不得收编 `_RETIRED` 里的实例；`release_kind` 的忙判定
按具体键、不按整个类别。

*为什么*：`release_idle` 在锁内选候选、在锁外 stop，两步之间的窗口里一次新的
acquire 会拿到"马上就要被停掉"的句柄，那次检索以 502 收场——可自愈但无法追查。
锁序恒为 key_lock → `_LOCK`（键锁用 RLock，`lease` 重入 `acquire`），不得倒置。
*钉它的测试*：`test_knowledge_services.py::
test_release_cannot_stop_a_service_that_was_just_acquired`（stop 钩子确定性
复现交错）、`::test_release_kind_only_skips_the_busy_key_not_the_whole_kind`、
`::test_idle_unload_skips_busy_services`。
（把两处改动分别退回去，对应测试必红）。

### 2.33 计算设备的传递通道按后端而定，且必须钉在真实包上

CPU / Vulkan / OpenVINO / ROCm 四个官方 Windows 包 `--version` 完全相同、
`--help` 的 GPU flag 集合也逐字相同（实测 b11929）——**版本串与 help 无法区分
后端**。识别只能靠同级加速 DLL（`ggml-openvino.dll` / `ggml-hip.dll` /
`ggml-vulkan.dll`）与构建自己的 `--list-devices`。

- 设备名的解析正则必须要求**以数字结尾**，否则 `error: something broke` 这类
  报错行会被当成名为 `error` 的设备（写完即被测试抓出过）。
- OpenVINO 后端**不认 `--device`**（实测传了直接退出：`... chosen with the
  GGML_OPENVINO_DEVICE environment variable, not -dev`）。设备必须译成
  `GGML_OPENVINO_DEVICE` 环境变量（`OPENVINO1` → `GPU.0`，取自
  `--list-devices` 描述里的 `GGML_OPENVINO_DEVICE=` 前缀，并剥掉
  ` (selected)` 标记）；翻译不出来或多设备时必须 400 拒绝启动，不得猜。
- ROCm 构建在没有受支持 AMD 独显时会**静默退回 CPU**并正常就绪（实测
  ~6.8 tok/s，比 Vulkan GPU 慢一个数量级）；实例就绪时必须给出
  `warnings` 提示（不是 `error_message`——后者在 RUNNING 态被渲染成红色错误）。
- 相关测试不得写死机器路径：`study/` 里有真实包就对它们断言，没有就 skip
  （`test_multibackend_runtimes.py`）。

*钉它的测试*：`test_device_backends.py`（设备翻译/`(selected)`/argv 通道/
DLL 判别）、`test_multibackend_runtimes.py`（四包归属与设备枚举）、
`test_runtime_path_sync.py`（路径双向同步）。

### 2.34 文档、配置与工作流必须与"产品实际提供的东西"一致

**任何一份会公开的文档里提到的路径/命令/产物，都必须是这个仓库真的有的东西。**
指一个被刻意排除的文件、跑一个没发布的脚本、要求一个已移除的产物，都是缺陷——
读者会照做，然后撞墙，而所有测试仍然全绿。

已实际踩过的四类（2026-11-05 一轮审计全部命中）：

1. **文档指向被排除的文件**：`CHANGELOG.md` 让读者看 `docs/RISK_ASSESSMENT.md`、
   `docs/STATUS.md`；`docs/KNOWLEDGE_BASE.md` 让读者 `python
   tools/eval_knowledge_retrieval.py`；`FRONTEND_CONTRACT.md` 让贡献者跑
   `tools/ui_preview.py`。这些文件全在 `CURATED_EXCLUDES` 里，公开树里不存在。
2. **注释指向被排除的文档**：`backend/api/*.py` 写"见 `docs/ROADMAP.md` §P1"。
   源码也是公开的，注释同样是给读者看的。
3. **文档与工作流互相矛盾**：README 说"本版不提供 NSIS 安装包"，而
   `release.yml` 硬性要求"恰好 1 个安装包"否则 `throw` —— 按文档打 tag 发布会
   直接失败。反过来也一样危险：配置留着但没人验收。
4. **`.gitignore` 漏了产物目录**：`.zip` 规则只覆盖归档本身，`release/` 目录
   （部署器把 launcher + backend 放这里）与 `release/dist/SHA256SUMS.txt` 不被
   任何规则命中。一次本地打包就会把 40 MB 产物留在工作树里，发布镜像门禁会把它
   当成"该发布的文件"。**审计 `.gitignore` 的正确方法是问"构建会写出哪些目录"，
   而不是问"哪些扩展名要忽略"。**

*为什么*：这类错误不会让任何测试变红——它们是"文档说 A、代码做 B"的静默漂移。
公开仓库的价值全在"clone 下来能直接用/能照文档做"，一份指错路的文档让整条
通道失效，而维护者看到的是全绿的 CI。

*怎么防*：改动文档/配置/工作流时，逐个把它们提到的路径与产物在当前工作树里
`ls` 一遍；`.github/workflows/*.yml`、`.gitignore`、`docs/*.md`、根 README 属于
"必须与实际一致"的清单。移除一个产品能力（通道、脚本、产物）时，必须在**同一次
改动**里清掉：工作流步骤、bundle/构建配置、测试断言（反转成"不得再出现"）、
README 的中英两份、`docs/RELEASE.md`/`BUILDING.md`/`GETTING_STARTED.md`、
以及本节 §4 的产品边界条目。

*钉它的测试*：
`test_release_mirror_gate.py::test_release_workflow_publishes_the_portable_zip_and_no_installer`
（双向断言 + 配置层校验）、
`test_packaging_contracts.py::test_build_rs_does_not_stage_a_bundle_resource_copy`。

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

镜像目录本身缺失时必须非 0 退出（只有在公开镜像自身里运行时才用
`--allow-missing-mirror` 显式跳过）；`--apply` 必须先做隐私/禁用规则预检，
任何命中都在**写入前**中止——不能先把私有内容复制进 `github/` 再报错。

**刻意不发布**（在 `CURATED_EXCLUDES` 里有规则，不是靠记忆）：
内部审计文档、AI 编码工具的配置与忽略文件、`data/`、`tmp/`、一切构建产物。

### 3.1 不要在 `github/` 里跑测试或 lint

验证"镜像能否独立构建"时，**不要 `cd github && pytest`**：一次运行就会留下
`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`，而 AI 编码工具的钩子还会写
`.mimosa/hook-state/*.json`——后者含用户目录下的绝对路径。
下一次门禁会报 121 条"禁用路径" + 115 条隐私命中（2026-11-03 实测），
看起来像镜像被污染了，其实只是刚才那次验证的副产物。

正确做法：`pytest --cache-clear -p no:cacheprovider` 并设
`PYTHONDONTWRITEBYTECODE=1`，或在**副本**里跑；跑完无论如何都要复查门禁。
`__pycache__` 与缓存目录不在镜像清单里，所以它们不会让"缺失/漂移"变红，
只会从"禁用路径/隐私面"侧面炸出来——这两种红字的原因完全不同，别混淆。

## 4. 产品边界（不要重新提议）

- **不打包、不自动下载 `llama-server`。** 每人环境不同，llama.cpp 自己按环境
  发布了预构建版本；默认路径留空 + README 指引获取方式，是刻意的。
- **双通道是硬要求**：既能 clone 源码自己编译，也能拿到装好即用的**免安装**
  便携包（`release/*-portable-x64.zip`）。只在一条通道上工作的改动算缺陷。
- **不提供安装包（NSIS 已移除，2026-11-05 决定）。** 这不是"某次发版不必交"，
  而是**通道整体删除**：`src-tauri/nsis/` 已删，`tauri.conf.json` 的
  `bundle.active` 为 `false` 且没有 `targets`，发布工作流用
  `tauri build --no-bundle`，`release.yml` 还会在产物里出现 `.exe` 时**主动失败**。
  理由：未签名的 per-user 安装包带来独立的 SmartScreen 拦截、卸载钩子、沙箱/
  受限令牌下的目录权限问题，而它面向的人群与便携包完全相同；维护两条分发链路
  却没有第二条的验收能力（安装/卸载往返需要干净机器，长期做不到），收益低于风险。
  早先（2026-10-28）的口径是"非必交、保留配置随时可重建"，实践下来那条口径
  只会让文档与工作流互相矛盾（README 说"不提供"、`release.yml` 却硬性要求
  "恰好 1 个安装包"否则发布失败）。**要恢复必须先同时具备**：代码签名
  （1.0 门槛）+ 干净机器上的安装/卸载往返验收 + 卸载钩子（清理 `webview2-data/`、
  `runtime-temp/`、`logs/`、`launcher-boot.log`、`debug.log`、
  `backend/backend-child.log`，且**绝不**删 `backend/data` 与 `%APPDATA%`/`%LOCALAPPDATA%`
  的用户数据）。三者缺一就不要重新引入。
  钉它的测试：`test_release_mirror_gate.py::test_release_workflow_publishes_the_portable_zip_and_no_installer`
  （双向：既钉便携包发布，也钉"不得再产安装包"——包括 `bundle.active` 必须为
  false、`targets` 不得回来、`nsis/` 不得回来、`--no-bundle` 必须在命令里）。
- 参数界面**跟随本机那份构建的能力**（`server_probe` + `capabilities`），
  不要把 llama.cpp 的 flag 列表当常量抄进代码。

## 5. 前端约定

- 新页面的空态一律用 `panel_views.emptyStateHtml`（它区分"目录未配置"与
  "无匹配"两种引导）。
- **页面列表长在侧栏里，不是独立的一列。** 页面模块导出
  `renderSidebarList(el, ctx)` 提供列表；异步数据到达后必须调用
  `appCtx.refreshList()`（旧名 `refreshPanel` 仍作为别名存在，但新代码用新名），
  否则列表会停在"加载中"。逐字符输入的场景用 `appCtx.refreshListKeepFocus()`，
  它会保住搜索框焦点与光标。
- 列表项用 `.panel-list-item` + `data-list-key`，**不能**用 `.sidebar-item`：
  后者是页面入口的选择器（`navigateTo` 绑定、导航计数、`aria-current` 都按它查）。
- 后端错误消息一律经 `i18n.localizeBackendError` 呈现；新增后端错误文案时
  **必须**同时补 `ERROR_PATTERNS` 与中英两份字典（有 key 奇偶性测试兜底）。
- 令牌只经 Tauri IPC 获取（`get_backend_session`），不得出现在前端源码或 URL 里。

## 6. 验证纪律（本项目按这条验收）

1. 声称"修好了"之前，必须给出**跑过的命令与它的输出**，不是推理。
2. 回归测试要做**红-绿**：把修复退回错误版本，测试必须变红；
   只"写完就绿"的测试可能什么都没测（本项目出现过两次）。
3. UI 改动要真开浏览器看渲染，console 里的报错算失败；纯接口测试测不到
   同源/跨源差异、测不到 WebSocket 握手、测不到 PowerShell 版本差异。
4. **"全绿"不等于"能用"。测试替身证明不了产品可用性。**
   2026-11-03 事故：报告了"903 测试全绿、打包后端可启动"，用户实测却发现
   **换模型发不出消息、新建会话必失败、右栏压住正文、应用关不掉**。四个缺陷
   全都在真实路径上，而当时没有任何一条测试跑过"真实 llama-server + 真实
   WebView + 真实操作序列"：

   - `TestClient` 不经过浏览器 CORS 层，所以"跨源 500 缺 CORS 头 →
     `Failed to fetch`"这条链永远测不到（`test_auth_coverage.py` 全部同源）；
   - `FakePopen` 不会返回 500，所以"数据库只读 → 所有保存都失败"测不到；
   - 断言 `.chat-inspector` 存在，测不到它右边界跑到 1052px 而容器只有 900px。

   因此：**凡是"用户会看到"的行为，验证必须用真实后端 + 真实浏览器/窗口跑一遍
   那条路径**，并读**存储状态与几何数值**（`getBoundingClientRect`、DB 行数），
   不是读界面文本、也不是读"元素是否存在"。替身与 TestClient 只用于单元层回归。
   现有接缝：`tools/dev_backend.py`（隔离数据目录）、
   `frontend/tests/browser/layout.test.mjs`（真实几何）、
   `LUNARSHROUD_TEST_SERVER`（可选真实 llama-server 路径，没给就整条 skip）。

5. **错误绝不能因为落库失败而被伪装成网络故障。** 2026-11-03 实测：三个
   SQLite 库同时只读（日志里 `readonly database` 出现 **116 次**），启动期
   `init_db()` 的失败被 `except Exception` 吞掉，后端照常起来、界面完全正常，
   而每次保存都 500；前端因为跨域 500 缺 CORS 头只看到 `Failed to fetch`。
   三层掩盖叠加，用户拿到的信息量是零。所以：存储层错误必须翻译成带
   `code` 的 503，**所有错误响应都要带 CORS 头**（`main.py`
   `_storage_error_response` + `unhandled_error_handler`），并且启动时用
   真实写事务探测（`sqlite_recovery.probe_writable`）把"不可写"记成 ERROR。

6. **错误文案映射不得吃掉真实字段名。** 同一天的第二层掩盖：`seed: null`
   触发 `seed must be a number`，而 `i18n.mjs` 的宽泛正则
   `/must be a number|must be a string|.../` 把它翻译成"知识库设置无效" ——
   用户根本没有知识库。通用类型错误现在走 `err.invalidValue` 并**保留后端
   原文**，知识库专属正则只匹配真正与知识库有关的措辞。

7. 测试不得写真实 `data/`。`backend/conftest.py` 在任何测试模块导入 `config`
   之前把 `DATA_DIR` / `HISTORY_DB` / `PRESETS_DIR` 重定向到会话级临时目录；
   进入 lifespan 的测试会建库、清洗密钥并收口历史状态，一旦漏掉就会改写用户
   数据（2026-10-17 实测发生过）。`test_regressions.py::test_test_suite_never_uses_the_repository_data_directory`
   钉住这一性质。
8. **测试必须自密闭，不得依赖继承的 `%TEMP%` 可写。** 受限令牌/沙箱下测试
   二进制写 `%TEMP%` 会被拒绝（2026-10-18 核验：同一份代码在作者机器 20/20、
   在核验环境 16/20）。测试目录应放在测试二进制旁边（Cargo 刚写过、必然可写）
   或由夹具注入；清理失败要记录，不能用 `let _ =` 静默吞掉。
   证据：把 `TEMP`/`TMP` 指向不存在的路径后直接运行 Rust 测试二进制，
   `22/22` 通过。
9. **本机验收套件不得并行跑，也不得写死机器路径。**
   `frontend/tests/browser/deep-paths.test.mjs` 与 `adversarial.test.mjs` 会对着
   **同一个后端的共享状态**建库/索引/删库，两个文件同时跑会互相看到对方的中间态
   （实测：并行跑时一个守卫误报“库消失了”，其实是另一个套件的时序）。用
   `--test-concurrency=1`。机器相关的输入只能来自环境变量
   （`LUNARSHROUD_TEST_EMBEDDING` / `LUNARSHROUD_TEST_KB`），没给就整条跳过 ——
   把绝对路径写进测试文件会把私人目录结构发布进 `github/` 镜像，而且让测试
   “只在一台机器上成立”。
10. **UI 验收必须读存储状态，不能只读界面文本。** 面板按自己的节奏轮询，一次
   小索引可能在下一个请求之前就完成；只看“界面变成空闲”既会在旧文本上立刻为真
   （假绿），也会在永远等不到闪现的中间态时超时（假红）。断言要读
   `GET /api/knowledge/{id}` 的 `status` + `updated_at`，再给界面一点追上来的
   时间。本项目 2026-10-23 两条都踩到了。
