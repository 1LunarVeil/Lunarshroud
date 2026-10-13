// LunarShroud - Tauri 主进程
//
// 职责：
// 1. 启动 Python FastAPI sidecar（管理 llama-server 实例）
// 2. 启动策略：
//    a) 优先查找 backend.exe（PyInstaller 打包的独立可执行文件）
//    b) 降级：python main.py（开发模式）
// 3. 等待 sidecar 就绪（端口探测 + 会话匹配）
// 4. 创建主窗口加载本地前端（asset:// 协议）
// 5. 监管 sidecar：异常退出后自动重启（复用同一会话令牌，前端可自动恢复）
// 6. 退出时清理：关闭 Job Object 回收所有子进程

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{Manager, WebviewWindowBuilder};

// Windows 平台：阻止子进程弹出控制台窗口
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

const BACKEND_READINESS_TIMEOUT: Duration = Duration::from_secs(30);

/// Windows 原生错误弹窗（支持中文，无乱码）
#[cfg(windows)]
fn show_error_box(title: &str, message: &str) {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK};

    let title_w: Vec<u16> = OsStr::new(title).encode_wide().chain(Some(0)).collect();
    let msg_w: Vec<u16> = OsStr::new(message).encode_wide().chain(Some(0)).collect();

    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            msg_w.as_ptr(),
            title_w.as_ptr(),
            MB_ICONERROR | MB_OK,
        );
    }
}

#[cfg(not(windows))]
fn show_error_box(title: &str, message: &str) {
    eprintln!("[{}] {}", title, message);
}

/// 给窗口补上任务栏图标（WM_SETICON 的 ICON_BIG 槽位）。
///
/// Tauri 只会设 ICON_SMALL：tao 的 `taskbar_icon` 只能通过 Windows 专用扩展 trait
/// `WindowBuilderExtWindows::with_taskbar_icon` 写入，而 Tauri 与 tauri-runtime-wry
/// 都不碰它，于是 tao 在建窗时对 BIG 执行的是 `unset_for_window`（显式置空）。
/// 任务栏因此只能回落到"取主模块图标"这条由图标缓存中介的路径——缓存一旦失效或
/// 被重建，Windows 就渲染成通用空白图标，表现为任务栏上一张白纸。
///
/// 这里用 exe 自己内嵌的多尺寸图标显式填上 BIG：tauri-build 以资源 id 32512
/// 写入 `icons/icon.ico`，LR_DEFAULTSIZE 让系统挑任务栏需要的尺寸，而不是像
/// Tauri 的默认窗口图标那样只能拿到 ico 的第一帧（16px）。
#[cfg(windows)]
fn apply_taskbar_icon(window: &tauri::WebviewWindow) {
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        LoadImageW, SendMessageW, ICON_BIG, IMAGE_ICON, LR_DEFAULTSIZE, WM_SETICON,
    };

    // tauri-build 写入 exe 的图标资源 id（等价于 MAKEINTRESOURCEW(32512)）
    const APP_ICON_RESOURCE: usize = 32512;

    let Ok(hwnd) = window.hwnd() else {
        return;
    };
    // windows-sys 0.59 里 HMODULE/HANDLE/HWND 是裸指针别名、WPARAM/LPARAM 是整数别名，
    // 所以这里不需要任何 newtype 包装。
    unsafe {
        let module = GetModuleHandleW(core::ptr::null());
        let icon = LoadImageW(
            module,
            APP_ICON_RESOURCE as *const u16,
            IMAGE_ICON,
            0,
            0,
            LR_DEFAULTSIZE,
        );
        if !icon.is_null() {
            SendMessageW(hwnd.0, WM_SETICON, ICON_BIG as usize, icon as isize);
        }
    }
}

#[cfg(not(windows))]
fn apply_taskbar_icon(_window: &tauri::WebviewWindow) {}

/// Backend connection details are kept out of the WebView source and injected at runtime.
#[derive(Clone, serde::Serialize)]
struct BackendSessionInfo {
    url: String,
    token: String,
}

struct BackendSession(BackendSessionInfo);

/// 当前这一代后端进程的可终止句柄（pid + Job Object）。
struct SupervisorShared {
    pid: Option<u32>,
    job: Option<usize>,
}

/// 后端监管器：跟踪当前存活的 backend 子进程，窗口关闭时终止它。
/// Job Object 句柄的唯一所有者是这里的共享状态，避免双重 CloseHandle。
struct BackendSupervisor {
    session_id: String,
    session_token: String,
    port: u16,
    shared: Mutex<SupervisorShared>,
    shutdown: AtomicBool,
}

impl BackendSupervisor {
    fn new(session_id: String, session_token: String, port: u16) -> Self {
        Self {
            session_id,
            session_token,
            port,
            shared: Mutex::new(SupervisorShared {
                pid: None,
                job: None,
            }),
            shutdown: AtomicBool::new(false),
        }
    }

    fn is_shutdown(&self) -> bool {
        self.shutdown.load(Ordering::SeqCst)
    }

    /// 登记新一代后端进程。若关闭已并发发生则返回 false，
    /// 此时已顺手关闭传入的 Job 句柄，调用方需自行回收子进程。
    fn register_generation(&self, pid: u32, job: Option<usize>) -> bool {
        let mut guard = self.shared.lock().unwrap();
        if self.is_shutdown() {
            if let Some(handle) = job {
                close_job_handle(handle);
            }
            return false;
        }
        guard.pid = Some(pid);
        guard.job = job;
        true
    }

    /// 关闭当前代际的 Job Object，回收仍存活的 llama-server 子进程。
    /// 可安全重复调用（句柄取出后即为空）。
    fn take_and_close_job(&self) {
        let mut guard = self.shared.lock().unwrap();
        if let Some(handle) = guard.job.take() {
            close_job_handle(handle);
        }
        guard.pid = None;
    }

    /// 窗口关闭路径：停止监管并终止当前后端进程树。
    fn request_shutdown(&self) {
        self.shutdown.store(true, Ordering::SeqCst);
        let (job, pid) = {
            let mut guard = self.shared.lock().unwrap();
            (guard.job.take(), guard.pid.take())
        };
        if let Some(handle) = job {
            close_job_handle(handle);
        } else if let Some(pid) = pid {
            // Job Object 附加失败时的兜底路径
            kill_process_tree(pid);
        }
        append_launcher_log("launcher shutdown requested");
    }
}

pub fn run() {
    // 与后端读取同一份持久化配置，保证用户修改 launcher_port 后，
    // Tauri 仍能在重启时探测到正确的 sidecar 端口。
    let port = configured_backend_port();
    // 后端以"外壳探测的同一端口"启动：backend/config.py 让该 env 优先于
    // config.json（仅内存生效、不落盘）。子进程继承本进程环境，监督器的
    // 重启代次同样生效 —— 外壳探测端口与后端监听端口永远来自同一个决策点，
    // dev 模式（backend 读项目 data/config.json、外壳读 %APPDATA%）不再可能分叉。
    std::env::set_var("LUNARSHROUD_LAUNCHER_PORT", port.to_string());

    // ========== 1. 查找并启动后端 ==========
    let session_id = random_session_secret().unwrap_or_else(|error| {
        show_error_box("LunarShroud 启动错误", &error);
        std::process::exit(1);
    });
    let session_token = random_session_secret().unwrap_or_else(|error| {
        show_error_box("LunarShroud 启动错误", &error);
        std::process::exit(1);
    });
    let (mut child, _backend_dir) = match start_backend(&session_id, &session_token) {
        Ok(result) => result,
        Err(e) => {
            // 启动失败时弹窗提示
            let msg = format!(
                "后端启动失败：{}\n\n请确保 backend.exe 或 Python 环境可用。",
                e
            );
            show_error_box("LunarShroud 启动错误", &msg);
            std::process::exit(1);
        }
    };

    let job_handle = attach_kill_on_close_job(child.id()).ok();

    // ========== 2. 等待后端就绪（首代失败即退出） ==========
    if !wait_backend_ready(
        &mut child,
        port,
        &session_id,
        BACKEND_READINESS_TIMEOUT,
    ) {
        cleanup_sidecar(&mut child, job_handle);
        let msg = format!(
            "后端启动超时（{}秒内未就绪）\n\n请检查端口 {} 是否被占用。",
            BACKEND_READINESS_TIMEOUT.as_secs(),
            port
        );
        show_error_box("LunarShroud 启动错误", &msg);
        std::process::exit(1);
    }
    append_launcher_log(&format!("backend ready pid={} port={}", child.id(), port));

    // ========== 3. 移交监管线程（崩溃后自动重启） ==========
    let supervisor = Arc::new(BackendSupervisor::new(
        session_id.clone(),
        session_token.clone(),
        port,
    ));
    supervisor.register_generation(child.id(), job_handle);
    std::thread::spawn({
        let supervisor = Arc::clone(&supervisor);
        move || supervise_loop(supervisor, child)
    });

    // ========== 4. 启动 Tauri 应用 ==========
    let backend_url = format!("http://{}:{}", "127.0.0.1", port);

    let backend_session_state = BackendSession(BackendSessionInfo {
        url: backend_url.clone(),
        token: session_token.clone(),
    });

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(supervisor.clone())
        .manage(backend_session_state)
        .invoke_handler(tauri::generate_handler![
            get_backend_session,
            open_file_dialog,
            open_directory_dialog
        ])
        .setup(move |app| {
            // 创建主窗口，加载本地前端文件（asset:// 协议）
            let main_window =
                WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()))
                    .title("LunarShroud")
                    .inner_size(1280.0, 820.0)
                    .min_inner_size(1024.0, 640.0)
                    .resizable(true)
                    .decorations(true)
                    .center()
                    .visible(true)
                    // 仅 debug 构建启用 DevTools；release 构建不暴露远程调试端口
                    .devtools(cfg!(debug_assertions))
                    .build()?;
            apply_taskbar_icon(&main_window);

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if let Some(supervisor) = window.app_handle().try_state::<Arc<BackendSupervisor>>()
                {
                    supervisor.request_shutdown();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

/// 监管循环：等待当前后端退出；非关闭原因退出时自动重启新一代。
/// 后端崩溃时其 llama-server 子进程可能仍在运行（Job 句柄未关），
/// 每代结束后关闭该代 Job Object 将它们一并回收。
fn supervise_loop(supervisor: Arc<BackendSupervisor>, mut child: std::process::Child) {
    loop {
        let exit = child.wait();
        supervisor.take_and_close_job();
        if supervisor.is_shutdown() {
            append_launcher_log(&format!("backend exited during shutdown: {exit:?}"));
            return;
        }
        append_launcher_log(&format!(
            "backend exited unexpectedly: {exit:?}; restarting"
        ));
        match spawn_ready_generation(&supervisor) {
            Some(next_child) => child = next_child,
            None => return,
        }
    }
}

/// 启动并等待新一代后端就绪。启动/就绪失败按指数退避重试；
/// 关闭请求赢得竞争时返回 None。
fn spawn_ready_generation(supervisor: &BackendSupervisor) -> Option<std::process::Child> {
    let mut failures: u32 = 0;
    loop {
        if supervisor.is_shutdown() {
            return None;
        }
        let (mut child, _backend_dir) =
            match start_backend(&supervisor.session_id, &supervisor.session_token) {
                Ok(result) => result,
                Err(error) => {
                    append_launcher_log(&format!("backend restart failed: {error}"));
                    std::thread::sleep(supervision_backoff(failures));
                    failures = failures.saturating_add(1);
                    continue;
                }
            };
        let job = attach_kill_on_close_job(child.id()).ok();
        if !supervisor.register_generation(child.id(), job) {
            // 关闭请求先到：登记被拒，Job 句柄已在 register 内关闭
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        if wait_backend_ready(
            &mut child,
            supervisor.port,
            &supervisor.session_id,
            BACKEND_READINESS_TIMEOUT,
        ) {
            append_launcher_log(&format!("backend restarted pid={}", child.id()));
            return Some(child);
        }
        append_launcher_log("backend restart did not become ready; retrying");
        supervisor.take_and_close_job();
        let _ = child.kill();
        let _ = child.wait();
        if supervisor.is_shutdown() {
            return None;
        }
        std::thread::sleep(supervision_backoff(failures));
        failures = failures.saturating_add(1);
    }
}

/// 重启退避：1s → 2s → 4s → 8s → 16s → 30s 封顶。
fn supervision_backoff(failures: u32) -> Duration {
    let seconds = 1u64.checked_shl(failures).unwrap_or(u32::MAX as u64);
    Duration::from_secs(seconds.min(30))
}

/// 就绪探测 URL。抽成纯函数是为了能被单测盯住两件事：打的是 `/api/system/ready`
/// （不需要令牌的端点），且**查询串里只有 session_id、没有令牌**。
fn ready_probe_url(port: u16, session_id: &str) -> String {
    format!("http://127.0.0.1:{port}/api/system/ready?session={session_id}")
}

/// 轮询后端就绪端点直到会话匹配。子进程提前退出或超时返回 false。
///
/// **绝不在这条探测上携带会话令牌。** 令牌是整套管理 API 的钥匙，而对端身份在
/// 第一次探测时还没被确认：任何占住该端口的本地进程都会收到它（Windows 的回环
/// 端口不按用户隔离，任意进程都能 bind 127.0.0.1:17520）。改用 session_id 反向
/// 证明身份 —— 它与令牌同为每次启动新生成的 256 位随机值，只经环境传给我们自己
/// 拉起的子进程，因此"能答对"就等价于"对端是我们的后端"，而它本身不是凭证。
fn wait_backend_ready(
    child: &mut std::process::Child,
    port: u16,
    session_id: &str,
    timeout: Duration,
) -> bool {
    let url = ready_probe_url(port, session_id);
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .expect("failed to create backend health client");

    let start = Instant::now();
    while start.elapsed() < timeout {
        if let Ok(Some(status)) = child.try_wait() {
            eprintln!("backend exited before readiness: {status}");
            append_launcher_log(&format!("backend exited before readiness: {status}"));
            return false;
        }
        if let Ok(resp) = client.get(&url).send() {
            if resp.status().is_success() {
                // 200 已经意味着 session_id 答对了；再核一次响应体，避免把
                // "某个恰好返回 200 的服务"误认成自己的后端。
                if let Ok(body) = resp.text() {
                    if backend_session_matches(&body, session_id) {
                        return true;
                    }
                }
            }
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

/// 从 JSON 配置内容解析启动器端口。
/// 单独抽出纯函数，便于覆盖合法、缺失和非法配置值。
fn parse_backend_port(contents: &str) -> Option<u16> {
    let value: serde_json::Value = serde_json::from_str(contents).ok()?;
    let port = value.get("launcher_port")?.as_u64()?;
    (1..=65535).contains(&port).then_some(port as u16)
}

/// 读取 %APPDATA%/LunarShroud/config.json 中的启动器端口。
/// 首次运行或配置损坏时回退到后端默认端口。
fn configured_backend_port() -> u16 {
    const DEFAULT_PORT: u16 = 17520;
    let appdata = match std::env::var_os("APPDATA") {
        Some(value) => PathBuf::from(value),
        None => return DEFAULT_PORT,
    };
    let config_path = appdata.join("LunarShroud").join("config.json");
    std::fs::read_to_string(config_path)
        .ok()
        .and_then(|contents| parse_backend_port(&contents))
        .unwrap_or(DEFAULT_PORT)
}

fn backend_session_matches(contents: &str, expected_session_id: &str) -> bool {
    serde_json::from_str::<serde_json::Value>(contents)
        .ok()
        .and_then(|value| value.get("session_id")?.as_str().map(str::to_owned))
        .is_some_and(|session_id| session_id == expected_session_id)
}

fn random_session_secret() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes)
        .map_err(|error| format!("无法生成安全的后端会话令牌: {error}"))?;
    Ok(bytes.iter().map(|byte| format!("{:02x}", byte)).collect())
}

#[cfg(windows)]
fn attach_kill_on_close_job(pid: u32) -> Result<usize, String> {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
    };

    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err(format!(
                "CreateJobObjectW failed: {}",
                std::io::Error::last_os_error()
            ));
        }

        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &limits as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) == 0
        {
            let error = std::io::Error::last_os_error();
            CloseHandle(job);
            return Err(format!("SetInformationJobObject failed: {}", error));
        }

        let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
        if process.is_null() {
            let error = std::io::Error::last_os_error();
            CloseHandle(job);
            return Err(format!("OpenProcess failed: {}", error));
        }
        let assigned = AssignProcessToJobObject(job, process);
        CloseHandle(process);
        if assigned == 0 {
            let error = std::io::Error::last_os_error();
            CloseHandle(job);
            return Err(format!("AssignProcessToJobObject failed: {}", error));
        }
        Ok(job as usize)
    }
}

#[cfg(not(windows))]
fn attach_kill_on_close_job(_pid: u32) -> Result<usize, String> {
    Err("job objects are only available on Windows".to_string())
}

#[cfg(windows)]
fn close_job_handle(handle: usize) {
    use windows_sys::Win32::Foundation::CloseHandle;
    unsafe {
        CloseHandle(handle as *mut core::ffi::c_void);
    }
}

#[cfg(not(windows))]
fn close_job_handle(_handle: usize) {}

/// 首代启动失败的兜底清理（监管线程尚未接管时使用）。
fn cleanup_sidecar(child: &mut std::process::Child, job: Option<usize>) {
    if let Some(handle) = job {
        close_job_handle(handle);
        let _ = child.try_wait();
    } else {
        kill_process_tree(child.id());
    }
}

/// 启动后端进程
///
/// 策略：
/// 1. 查找 backend.exe（PyInstaller 打包，独立可执行文件）
/// 2. 如果找不到，尝试 python main.py（开发模式）
///
/// 返回 (子进程句柄, 工作目录)
fn start_backend(
    session_id: &str,
    session_token: &str,
) -> Result<(std::process::Child, std::path::PathBuf), String> {
    // ── 策略 A：查找 backend.exe ──
    if let Some(backend_exe) = find_backend_exe() {
        let backend_dir = backend_exe.parent().unwrap_or(&backend_exe).to_path_buf();
        let mut cmd = Command::new(&backend_exe);
        cmd.current_dir(&backend_dir)
            .env("LUNARSHROUD_SESSION_ID", session_id)
            .env("LUNARSHROUD_SESSION_TOKEN", session_token)
            // stdout/stderr 置空：日志由后端自行处理（WebSocket 推送），
            // 避免管道无人读取导致缓冲填满阻塞后端进程
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);
        let child = cmd
            .spawn()
            .map_err(|e| format!("无法启动 backend.exe: {}", e))?;
        return Ok((child, backend_dir));
    }

    #[cfg(not(debug_assertions))]
    return Err("发布包缺少受信任的 backend/backend.exe 资源".to_string());

    #[cfg(debug_assertions)]
    {
        // ── 策略 B：降级到 python main.py（开发模式） ──
        let backend_dir = find_backend_dir();
        let python_exe = find_python();

        let main_py = backend_dir.join("main.py");
        if !main_py.exists() {
            return Err("找不到 backend/main.py 也找不到 backend.exe\n\n".to_string());
        }

        let mut cmd = Command::new(&python_exe);
        cmd.arg(&main_py)
            .current_dir(&backend_dir)
            .env("LUNARSHROUD_SESSION_ID", session_id)
            .env("LUNARSHROUD_SESSION_TOKEN", session_token)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);
        let child = cmd.spawn().map_err(|e| {
        format!("无法启动 Python 后端: {}\n请确保已安装 Python 3.11+ 并执行 pip install -r requirements.txt", e)
    })?;
        Ok((child, backend_dir))
    }
}

/// 查找 backend.exe
///
/// Release only accepts the bundled `backend/backend.exe`. Debug builds may
/// additionally use a backend under the current working directory.
fn find_backend_exe() -> Option<std::path::PathBuf> {
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))?;

    // Release only trusts the resource staged beside the application binary.
    let bundled = exe_dir.join("backend").join("backend.exe");
    if bundled.is_file() {
        let canonical_exe_dir = exe_dir.canonicalize().ok()?;
        let canonical_backend = bundled.canonicalize().ok()?;
        if canonical_backend.starts_with(canonical_exe_dir.join("backend")) {
            return Some(canonical_backend);
        }
    }

    #[cfg(debug_assertions)]
    if let Ok(cwd) = std::env::current_dir() {
        let candidate = cwd.join("backend").join("backend.exe");
        if candidate.is_file() {
            return candidate.canonicalize().ok();
        }
    }

    None
}

/// 查找 backend/main.py 目录（开发模式降级用）
#[cfg(debug_assertions)]
fn find_backend_dir() -> std::path::PathBuf {
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))
        .unwrap_or_else(|| std::path::PathBuf::from("."));

    // 从 exe 目录向上逐级查找
    let mut current: Option<std::path::PathBuf> = Some(exe_dir.clone());
    while let Some(dir) = current {
        let candidate = dir.join("backend");
        let main_py = candidate.join("main.py");
        if main_py.exists() {
            return candidate;
        }
        current = dir.parent().map(|p| p.to_path_buf());
    }

    // 兜底：当前工作目录的 backend
    std::env::current_dir()
        .map(|p| p.join("backend"))
        .unwrap_or_else(|_| std::path::PathBuf::from("backend"))
}

/// 查找系统中的 Python 可执行文件（开发模式降级用）
#[cfg(debug_assertions)]
fn find_python() -> String {
    // 优先级：python > python3 > py
    for name in &["python", "python3", "py"] {
        let mut cmd = Command::new("where");
        cmd.arg(name);
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);
        if let Ok(output) = cmd.output() {
            if output.status.success() {
                let stdout = String::from_utf8_lossy(&output.stdout);
                if let Some(first_line) = stdout.lines().next() {
                    return first_line.trim().to_string();
                }
            }
        }
    }
    "python".to_string()
}

/// System32 里某个工具的绝对路径；拿不到 SystemRoot 时退回裸名。
///
/// 系统工具**必须**用绝对路径调用。裸名会走 CreateProcess 的搜索序，而
/// "应用程序所在目录"与"当前目录"都排在 System32 之前 —— 外壳的 CWD 来自快捷
/// 方式的 WorkingDirectory（Publish-PortableRuntime.ps1:88 把它设成用户可写的
/// `<Dest>`），实测在那里植入 taskkill.exe 后，跑的是植入副本。
fn system_tool(name: &str) -> PathBuf {
    #[cfg(windows)]
    {
        let root = std::env::var_os("SystemRoot").or_else(|| std::env::var_os("windir"));
        if let Some(root) = root {
            return PathBuf::from(root).join("System32").join(name);
        }
    }
    PathBuf::from(name)
}

/// 杀掉进程树（包括所有子进程）
fn kill_process_tree(pid: u32) {
    // Windows: taskkill /F /T /PID（绝对路径，见 system_tool）
    let mut cmd = Command::new(system_tool("taskkill.exe"));
    cmd.args(["/F", "/T", "/PID", &pid.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    let _ = cmd.status();
}

/// 启动器侧事件日志：%APPDATA%/LunarShroud/logs/launcher.log。
/// 与后端 backend.log 同目录，便于一起取证。写入失败静默忽略。
fn launcher_log_path() -> Option<PathBuf> {
    let appdata = std::env::var_os("APPDATA")?;
    let mut path = PathBuf::from(appdata);
    path.push("LunarShroud");
    path.push("logs");
    path.push("launcher.log");
    Some(path)
}

fn append_launcher_log_at(path: &Path, timestamp_secs: u64, message: &str) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    writeln!(file, "[{timestamp_secs}] {message}")
}

fn append_launcher_log(message: &str) {
    let Some(path) = launcher_log_path() else {
        return;
    };
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    let _ = append_launcher_log_at(&path, timestamp, message);
}

#[tauri::command]
fn get_backend_session(state: tauri::State<BackendSession>) -> BackendSessionInfo {
    state.0.clone()
}

/// 将 FilePath 枚举转换为字符串路径
fn filepath_to_string(fp: tauri_plugin_dialog::FilePath) -> Option<String> {
    match fp {
        tauri_plugin_dialog::FilePath::Path(p) => Some(p.to_string_lossy().to_string()),
        tauri_plugin_dialog::FilePath::Url(u) => u
            .to_file_path()
            .ok()
            .map(|p| p.to_string_lossy().to_string()),
    }
}

/// Tauri command：打开文件选择对话框（异步回调包装，避免阻塞 WebView2 主线程）
#[tauri::command]
async fn open_file_dialog(app: tauri::AppHandle) -> Option<String> {
    use tauri_plugin_dialog::DialogExt;
    let (tx, rx) = tokio::sync::oneshot::channel();
    app.dialog().file().pick_file(move |path| {
        let _ = tx.send(path);
    });
    let file_path = rx.await.ok().flatten();
    file_path.and_then(filepath_to_string)
}

/// Tauri command：打开目录选择对话框（异步回调包装，避免阻塞 WebView2 主线程）
#[tauri::command]
async fn open_directory_dialog(app: tauri::AppHandle) -> Option<String> {
    use tauri_plugin_dialog::DialogExt;
    let (tx, rx) = tokio::sync::oneshot::channel();
    app.dialog().file().pick_folder(move |path| {
        let _ = tx.send(path);
    });
    let folder_path = rx.await.ok().flatten();
    folder_path.and_then(filepath_to_string)
}

#[cfg(test)]
mod tests {
    use super::{
        append_launcher_log_at, backend_session_matches, parse_backend_port, random_session_secret,
        ready_probe_url, supervision_backoff, system_tool, BackendSupervisor,
    };
    use std::time::Duration;

    #[test]
    fn parses_valid_backend_port() {
        assert_eq!(
            parse_backend_port(r#"{"launcher_port":17620}"#),
            Some(17620)
        );
    }

    #[test]
    fn rejects_missing_or_invalid_backend_port() {
        assert_eq!(parse_backend_port(r#"{"theme":"dark"}"#), None);
        assert_eq!(parse_backend_port(r#"{"launcher_port":0}"#), None);
        assert_eq!(parse_backend_port(r#"{"launcher_port":70000}"#), None);
        assert_eq!(parse_backend_port("not-json"), None);
    }

    #[test]
    fn backend_readiness_requires_the_current_session() {
        let body = r#"{"status":"ok","session_id":"fresh-session","pid":123}"#;
        assert!(backend_session_matches(body, "fresh-session"));
        assert!(!backend_session_matches(body, "stale-session"));
        assert!(!backend_session_matches("not-json", "fresh-session"));
    }

    #[test]
    fn session_secrets_are_random_256_bit_hex_values() {
        let first = random_session_secret().expect("OS random generator should be available");
        let second = random_session_secret().expect("OS random generator should be available");
        assert_eq!(first.len(), 64);
        assert!(first.chars().all(|value| value.is_ascii_hexdigit()));
        assert_ne!(first, second);
    }

    #[test]
    fn supervision_backoff_grows_and_caps_at_30s() {
        assert_eq!(supervision_backoff(0), Duration::from_secs(1));
        assert_eq!(supervision_backoff(1), Duration::from_secs(2));
        assert_eq!(supervision_backoff(2), Duration::from_secs(4));
        assert_eq!(supervision_backoff(3), Duration::from_secs(8));
        assert_eq!(supervision_backoff(4), Duration::from_secs(16));
        assert_eq!(supervision_backoff(5), Duration::from_secs(30));
        assert_eq!(supervision_backoff(40), Duration::from_secs(30));
    }

    #[test]
    fn launcher_log_appends_timestamped_lines() {
        let dir = std::env::temp_dir().join(format!("lunarshroud-log-test-{}", std::process::id()));
        let path = dir.join("logs").join("launcher.log");
        let _ = std::fs::remove_dir_all(&dir);

        append_launcher_log_at(&path, 1000, "backend restarted").unwrap();
        append_launcher_log_at(&path, 1001, "backend exited").unwrap();

        let contents = std::fs::read_to_string(&path).unwrap();
        assert!(contents.contains("[1000] backend restarted"));
        assert!(contents.contains("[1001] backend exited"));

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn generation_registration_refuses_after_shutdown() {
        let supervisor = BackendSupervisor::new("s".into(), "t".into(), 17520);
        supervisor.request_shutdown();
        assert!(!supervisor.register_generation(222, None));
        // 重复关闭请求必须安全（句柄已取出为空）
        supervisor.request_shutdown();
    }

    #[test]
    fn readiness_probe_carries_the_session_id_but_never_the_token() {
        // 就绪探测打的是不需要令牌的端点，凭 session_id 反向证明对端身份。
        // 曾经的做法是带着令牌去探 /api/system/health —— 那等于在确认"对面是
        // 谁"之前就把钥匙递出去，任何占住该端口的本地进程都能收到。
        let url = ready_probe_url(17520, "abc123");
        assert_eq!(url, "http://127.0.0.1:17520/api/system/ready?session=abc123");
        assert!(!url.contains("Token"), "{url}");
        assert!(!url.contains("health"), "{url}");
    }

    #[test]
    #[cfg(windows)]
    fn system_tools_are_resolved_inside_system32() {
        let tool = system_tool("taskkill.exe");
        assert!(tool.is_absolute(), "{tool:?}");
        assert!(tool.ends_with("System32\\taskkill.exe"), "{tool:?}");
    }

    #[test]
    #[cfg(not(windows))]
    fn system_tools_fall_back_to_the_bare_name() {
        assert_eq!(system_tool("taskkill.exe").to_string_lossy(), "taskkill.exe");
    }
}
