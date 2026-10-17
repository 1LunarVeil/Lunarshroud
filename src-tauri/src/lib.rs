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

use ring::hmac;
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

/// 单实例互斥锁。返回的句柄必须活到进程结束（调用方 mem::forget）。
///
/// 没有它时，重复双击会再起一套外壳 + 后端：第二个后端抢不到 17520，uvicorn
/// 以 exit 3 退出，用户看到"后端进程启动失败"——而第一个实例（窗口可能被其他
/// 窗口挡住/最小化）其实一直好好的。实测用户就是这么连续点出来的错误。
#[cfg(windows)]
fn acquire_single_instance() -> Option<usize> {
    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, SetLastError, ERROR_ALREADY_EXISTS,
    };
    use windows_sys::Win32::System::Threading::CreateMutexW;

    let name: Vec<u16> = "Local\\LunarShroud-SingleInstance\0"
        .encode_utf16()
        .collect();
    // CreateMutexW 只在"名字已存在"时设置 ERROR_ALREADY_EXISTS；进程启动时
    // LastError 可能残留其他 API 的值，必须先清零，否则会误判成"已在运行"。
    unsafe {
        SetLastError(0);
    }
    let handle = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
    if handle.is_null() {
        // 拿不到锁句柄时宁可放行，不要把应用永久锁死在启动失败上。
        return None;
    }
    if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
        unsafe {
            CloseHandle(handle);
        }
        return None;
    }
    Some(handle as usize)
}

/// 二次启动时把已有窗口拉到前台；找不到窗口才弹提示。
#[cfg(windows)]
fn focus_existing_window() -> bool {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        FindWindowW, SetForegroundWindow, ShowWindow, SW_RESTORE,
    };
    let title: Vec<u16> = "LunarShroud\0".encode_utf16().collect();
    let hwnd = unsafe { FindWindowW(std::ptr::null(), title.as_ptr()) };
    if hwnd.is_null() {
        return false;
    }
    unsafe {
        ShowWindow(hwnd, SW_RESTORE);
        SetForegroundWindow(hwnd);
    }
    true
}

#[cfg(not(windows))]
fn focus_existing_window() -> bool {
    false
}

#[cfg(not(windows))]
fn acquire_single_instance() -> Option<usize> {
    Some(1)
}

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

/// 某一代后端的会话凭据。**每一代都重新生成**：旧令牌在后端重启后立即作废，
/// 这样前端在"旧后端已退出、新后端尚未监听"的窗口里重连时，即使把缓存里的
/// 旧令牌发给了抢到端口的进程，它也拿不到任何仍然有效的东西。
///
/// 只保存令牌：session_id 只用于启动时的就绪探测，探测通过后外壳不再需要它，
/// 也就不该把它交给 WebView。
#[derive(Clone)]
struct ActiveSession {
    token: String,
}

/// 当前这一代后端进程的可终止句柄（pid + Job Object）。
struct SupervisorShared {
    pid: Option<u32>,
    job: Option<usize>,
}

/// 后端监管器：跟踪当前存活的 backend 子进程，窗口关闭时终止它。
/// Job Object 句柄的唯一所有者是这里的共享状态，避免双重 CloseHandle。
struct BackendSupervisor {
    port: u16,
    /// `None` = 当前没有**已通过就绪验证**的后端。`get_backend_session` 只在
    /// 这里为 `Some` 时把 URL/令牌交给前端，否则前端不得向该端口发送令牌。
    session: Mutex<Option<ActiveSession>>,
    shared: Mutex<SupervisorShared>,
    shutdown: AtomicBool,
}

impl BackendSupervisor {
    fn new(port: u16) -> Self {
        Self {
            port,
            session: Mutex::new(None),
            shared: Mutex::new(SupervisorShared {
                pid: None,
                job: None,
            }),
            shutdown: AtomicBool::new(false),
        }
    }

    /// 记录一代**已通过 HMAC 就绪校验**的后端会话（只保留令牌）。
    fn set_session(&self, token: String) {
        *self.session.lock().unwrap() = Some(ActiveSession { token });
    }

    /// 当前代退出/重启开始时调用：立刻让前端拿不到旧令牌。
    fn clear_session(&self) {
        *self.session.lock().unwrap() = None;
    }

    fn current_session(&self) -> Option<ActiveSession> {
        self.session.lock().unwrap().clone()
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
        self.clear_session();
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

/// 外部链接只允许这两个协议；`file:` / `ms-settings:` 等一律拒绝。
fn external_url_is_allowed(scheme: &str) -> bool {
    matches!(scheme, "http" | "https")
}

/// 用系统默认浏览器打开外部链接。
///
/// WebView2 的 `window.open` / `target="_blank"` 在没有 `on_new_window` 处理
/// 程序时会被 wry **直接拒绝**，链接看起来"点了没反应" —— 新手指南里的
/// llama.cpp 发布页、Chat 页的"在浏览器中打开"都属于这一类。真正的系统浏览器
/// 由 ShellExecuteW 拉起，而不是在应用内再开一个加载远程内容的 WebView。
#[cfg(windows)]
fn open_external_url(url: &str) -> Result<(), String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

    fn wide(value: &str) -> Vec<u16> {
        std::ffi::OsStr::new(value)
            .encode_wide()
            .chain(Some(0))
            .collect()
    }

    let operation = wide("open");
    let file = wide(url);
    let result = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            operation.as_ptr(),
            file.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            SW_SHOWNORMAL,
        )
    };
    // ShellExecuteW 的返回值 > 32 才表示成功（Win32 文档的既有约定）。
    if result as isize > 32 {
        Ok(())
    } else {
        Err(format!("ShellExecuteW failed: {}", result as isize))
    }
}

#[cfg(not(windows))]
fn open_external_url(_url: &str) -> Result<(), String> {
    Err("opening external URLs is only implemented on Windows".into())
}

pub fn run() {
    // 先写引导日志：即使后面的单实例判断/窗口创建卡住，也能留下证据。
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            write_boot_log(dir);
        }
    }
    let _instance_guard = match acquire_single_instance() {
        Some(handle) => handle,
        None => {
            if !focus_existing_window() {
                show_error_box(
                    "LunarShroud 已在运行",
                    "检测到另一个 LunarShroud 已经在运行（窗口可能在任务栏里、被其他窗口挡住或已最小化）。\n\n\
                     请先在任务栏/Alt+Tab 里找到它，不要重复启动。\n\
                     如果确定没有可见窗口，请在任务管理器结束残留的 LunarShroud.exe 和 backend.exe 后再试。",
                );
            }
            std::process::exit(0);
        }
    };
    // 独立于 APPDATA 的引导记录已在最前面写；这里不再重复。
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

    let job_handle = match attach_kill_on_close_job(child.id()) {
        Ok(handle) => Some(handle),
        Err(error) => {
            // F20：静默 .ok() 吞掉附加失败 = 强杀外壳时后端可能变孤儿；
            // 至少留下可诊断的告警（launcher.log），而不是无痕降级。
            append_launcher_log(&format!(
                "warning: kill-on-close Job attach failed for pid={}: {error}",
                child.id()
            ));
            None
        }
    };

    // ========== 2. 等待后端就绪（首代失败即退出） ==========
    if !wait_backend_ready(
        &mut child,
        port,
        &session_id,
        &session_token,
        BACKEND_READINESS_TIMEOUT,
    ) {
        let exit_status = child.try_wait().ok().flatten();
        cleanup_sidecar(&mut child, job_handle);
        let msg = match exit_status {
            Some(status) => format!(
                "后端进程启动失败（{status}），在就绪校验前退出。\n\n\
                 常见原因：临时目录不可写、被杀毒软件拦截，或 backend.exe 被替换/损坏。\n\
                 诊断日志：%APPDATA%\\LunarShroud\\logs\\launcher.log\n\
                 受限环境另有：应用目录下的 launcher-boot.log 与 backend\\backend-child.log"
            ),
            None => format!(
                "后端启动超时（{}秒内未就绪）\n\n请检查端口 {} 是否被占用。",
                BACKEND_READINESS_TIMEOUT.as_secs(),
                port
            ),
        };
        show_error_box("LunarShroud 启动错误", &msg);
        std::process::exit(1);
    }
    append_launcher_log(&format!("backend ready pid={} port={}", child.id(), port));

    // ========== 3. 移交监管线程（崩溃后自动重启） ==========
    let supervisor = Arc::new(BackendSupervisor::new(port));
    // 只有在 HMAC 就绪校验通过之后，前端才可能通过 get_backend_session 拿到
    // 这一代的 URL/令牌；这一代之后每次重启都会生成新的一对（见
    // spawn_ready_generation），旧令牌立即作废。
    supervisor.set_session(session_token.clone());
    supervisor.register_generation(child.id(), job_handle);
    std::thread::spawn({
        let supervisor = Arc::clone(&supervisor);
        move || supervise_loop(supervisor, child)
    });

    // ========== 4. 启动 Tauri 应用 ==========
    let window_log_dir = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf));
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(supervisor.clone())
        .invoke_handler(tauri::generate_handler![
            get_backend_session,
            open_file_dialog,
            open_directory_dialog
        ])
        .setup(move |app| {
            if let Some(dir) = &window_log_dir {
                let _ = append_launcher_log_at(
                    &dir.join("launcher-boot.log"),
                    now_secs(),
                    "setup: creating main window",
                );
            }
            // 创建主窗口，加载本地前端文件（asset:// 协议）
            let window_builder =
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
                    // WebView2 默认**拒绝** window.open / target="_blank"（wry 在
                    // 没有处理器时 SetHandled(true)）。外部 http(s) 链接交给系统
                    // 浏览器；应用内不再开第二个加载远程内容的窗口。
                    .on_new_window(|url, _features| {
                        if external_url_is_allowed(url.scheme()) {
                            if let Err(error) = open_external_url(url.as_str()) {
                                append_launcher_log(&format!(
                                    "failed to open external URL {url}: {error}"
                                ));
                            }
                        } else {
                            append_launcher_log(&format!(
                                "blocked external URL with unsupported scheme: {url}"
                            ));
                        }
                        tauri::webview::NewWindowResponse::Deny
                    });
            // WebView2 默认把用户数据放在 %LOCALAPPDATA%/<identifier>；受限
            // 会话/沙箱里该目录不可写时，CreateCoreWebView2Environment 直接
            // 返回 os error 5，窗口建不出来（用户看到的就是"没反应"）。把
            // WebView2 数据目录放到 exe 同目录（启动器已确认可写），窗口就能
            // 正常创建；缓存/本地存储在应用目录，不影响用户配置。
            let window_builder = match &window_log_dir {
                Some(dir) => window_builder.data_directory(dir.join("webview2-data")),
                None => window_builder,
            };
            let main_window = match window_builder.build() {
                Ok(window) => window,
                Err(error) => {
                    // 窗口创建失败（WebView2 缺失/用户数据目录不可写/运行时初始化
                    // 失败）以前只会让进程停在事件循环里、用户看不到任何窗口。
                    if let Some(dir) = &window_log_dir {
                        let _ = append_launcher_log_at(
                            &dir.join("launcher-boot.log"),
                            now_secs(),
                            &format!("setup: window creation failed: {error}"),
                        );
                    }
                    show_error_box(
                        "LunarShroud 启动错误",
                        &format!(
                            "主窗口创建失败：{error}\n\n\
                             常见原因：WebView2 Runtime 未安装/损坏，或 \
                             %LOCALAPPDATA% 不可写。\n\
                             诊断日志：应用目录下的 launcher-boot.log"
                        ),
                    );
                    std::process::exit(1);
                }
            };
            if let Some(dir) = &window_log_dir {
                let _ = append_launcher_log_at(
                    &dir.join("launcher-boot.log"),
                    now_secs(),
                    "setup: main window created",
                );
            }
            // 强制把窗口显示到前台。WebView2 在部分受限/远程桌面环境里会
            // 返回成功但窗口不可见；此时用户看不到任何界面，重复双击又触发
            // 第二个实例，最终只看到"后端启动失败（exit 3）"。这里用原生
            // ShowWindow/SetForegroundWindow 兜底，并把 HWND/可见性写进 boot 日志。
            #[cfg(windows)]
            {
                use windows_sys::Win32::UI::WindowsAndMessaging::{
                    IsWindowVisible, SetForegroundWindow, ShowWindow, SW_RESTORE,
                };
                if let Ok(hwnd) = main_window.hwnd() {
                    let raw = hwnd.0;
                    let visible_before = unsafe { IsWindowVisible(raw) } != 0;
                    unsafe {
                        ShowWindow(raw, SW_RESTORE);
                        SetForegroundWindow(raw);
                    }
                    let visible_after = unsafe { IsWindowVisible(raw) } != 0;
                    if let Some(dir) = &window_log_dir {
                        let _ = append_launcher_log_at(
                            &dir.join("launcher-boot.log"),
                            now_secs(),
                            &format!(
                                "setup: hwnd={raw:?} visible_before={visible_before} visible_after={visible_after}"
                            ),
                        );
                    }
                }
            }
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
        // 这一代已经死了：先让前端拿不到这一代的令牌，再决定是否重启。
        // 前端在重连窗口里只会拿到 None，因此不会把旧令牌发给抢先占端口的进程；
        // 即使它仍缓存着旧令牌，重启也会换发新的，旧值不可用于新一代后端。
        supervisor.clear_session();
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
///
/// **每一代都生成新的 session_id + token**。旧令牌随即失效，因此前端在
/// "旧后端已退出、新后端尚未监听"的重连窗口里即使把缓存令牌发给了抢到端口
/// 的本地进程，那个令牌对新一代后端也不再有效。
fn spawn_ready_generation(supervisor: &BackendSupervisor) -> Option<std::process::Child> {
    let mut failures: u32 = 0;
    loop {
        if supervisor.is_shutdown() {
            return None;
        }
        let session_id = match random_session_secret() {
            Ok(value) => value,
            Err(error) => {
                append_launcher_log(&format!(
                    "backend restart session generation failed: {error}"
                ));
                return None;
            }
        };
        let session_token = match random_session_secret() {
            Ok(value) => value,
            Err(error) => {
                append_launcher_log(&format!(
                    "backend restart session generation failed: {error}"
                ));
                return None;
            }
        };
        let (mut child, _backend_dir) = match start_backend(&session_id, &session_token) {
            Ok(result) => result,
            Err(error) => {
                append_launcher_log(&format!("backend restart failed: {error}"));
                std::thread::sleep(supervision_backoff(failures));
                failures = failures.saturating_add(1);
                continue;
            }
        };
        let job = match attach_kill_on_close_job(child.id()) {
            Ok(handle) => Some(handle),
            Err(error) => {
                // F20：重启代次同样不能静默降级，见首代处的说明。
                append_launcher_log(&format!(
                    "warning: kill-on-close Job attach failed for pid={}: {error}",
                    child.id()
                ));
                None
            }
        };
        if !supervisor.register_generation(child.id(), job) {
            // 关闭请求先到：登记被拒，Job 句柄已在 register 内关闭
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        if wait_backend_ready(
            &mut child,
            supervisor.port,
            &session_id,
            &session_token,
            BACKEND_READINESS_TIMEOUT,
        ) {
            // 只有验证通过后才把这一代交给前端；get_backend_session 在此之前
            // 返回 None，前端不会向该端口发送任何令牌。
            supervisor.set_session(session_token);
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

/// 就绪证明的域分隔前缀，与 backend/main.py 的 `READY_PROOF_CONTEXT` 逐字节一致。
const READY_PROOF_CONTEXT: &[u8] = b"lunarshroud-ready-v1:";

/// 就绪探测 URL。抽成纯函数是为了能被单测盯住三件事：打的是
/// `/api/system/ready`（不需要令牌的端点）、查询串里只有会话号与一次性
/// nonce、**没有令牌**。
fn ready_probe_url(port: u16, session_id: &str, nonce: &str) -> String {
    format!("http://127.0.0.1:{port}/api/system/ready?session={session_id}&nonce={nonce}")
}

/// HMAC-SHA256(token, "lunarshroud-ready-v1:" + nonce) 的小写十六进制。
///
/// 后端用它回答外壳的挑战。令牌既不出现在请求里，也不出现在响应里；占住
/// 端口的本地进程即使看到 nonce 也没有令牌，算不出这个摘要。
fn readiness_proof(token: &str, nonce: &str) -> String {
    let key = hmac::Key::new(hmac::HMAC_SHA256, token.as_bytes());
    let mut message = Vec::with_capacity(READY_PROOF_CONTEXT.len() + nonce.len());
    message.extend_from_slice(READY_PROOF_CONTEXT);
    message.extend_from_slice(nonce.as_bytes());
    hmac::sign(&key, &message)
        .as_ref()
        .iter()
        .map(|byte| format!("{:02x}", byte))
        .collect()
}

/// 常量时间比较两个十六进制摘要，避免按字节短路泄漏前缀匹配长度。
fn secrets_equal(expected: &str, provided: &str) -> bool {
    let (expected, provided) = (expected.as_bytes(), provided.as_bytes());
    if expected.len() != provided.len() {
        return false;
    }
    let mut difference = 0u8;
    for (left, right) in expected.iter().zip(provided.iter()) {
        difference |= left ^ right;
    }
    difference == 0
}

/// 判断 ready 响应是不是**我们拉起的那个后端**给出的。
///
/// 仅匹配 `session_id` 是不够的：那个值就写在外壳发出的请求 URL 里，任何
/// 收得到请求的进程都能原样回显。真正的证明是 `proof` —— 只有持有会话令牌
/// 的后端才能对本次 nonce 算出正确摘要。令牌为空（不安全开发模式）时退化为
/// 只核对会话号：那种模式本来就没有可保护的秘密。
fn backend_readiness_is_authentic(
    contents: &str,
    expected_session_id: &str,
    expected_proof: Option<&str>,
) -> bool {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(contents) else {
        return false;
    };
    let session_matches = value
        .get("session_id")
        .and_then(|value| value.as_str())
        .is_some_and(|session_id| secrets_equal(expected_session_id, session_id));
    if !session_matches {
        return false;
    }
    match expected_proof {
        None => true,
        Some(expected) => value
            .get("proof")
            .and_then(|value| value.as_str())
            .is_some_and(|proof| secrets_equal(expected, proof)),
    }
}

/// 轮询后端就绪端点，直到它证明自己持有本次启动的令牌。
///
/// **绝不在这条探测上携带会话令牌**：令牌是整套管理 API 的钥匙，而对端身份
/// 在第一次探测时还没被确认 —— 任何占住该端口的本地进程都会收到它（Windows
/// 的回环端口不按用户隔离，任意进程都能 bind 127.0.0.1:17520）。改用
/// 挑战-应答：外壳每次探测生成一个新的 256 位 nonce，只有持令牌的后端能算出
/// `HMAC-SHA256(token, context+nonce)`；占端口的进程只看到问题、看不到答案。
fn wait_backend_ready(
    child: &mut std::process::Child,
    port: u16,
    session_id: &str,
    session_token: &str,
    timeout: Duration,
) -> bool {
    let nonce = match random_session_secret() {
        Ok(value) => value,
        Err(error) => {
            append_launcher_log(&format!("readiness nonce generation failed: {error}"));
            return false;
        }
    };
    let url = ready_probe_url(port, session_id, &nonce);
    let expected_proof = if session_token.is_empty() {
        None
    } else {
        Some(readiness_proof(session_token, &nonce))
    };
    // no_proxy: the probe targets 127.0.0.1. reqwest otherwise honours
    // HTTP_PROXY/HTTPS_PROXY from the environment, so a dead system proxy
    // (Clash/V2Ray set one and the user did not start it yet) turns a healthy
    // backend into "did not become ready". Loopback traffic must never be
    // proxied. The backend's instance health probe carries the same fix.
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .no_proxy()
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
                if let Ok(body) = resp.text() {
                    if backend_readiness_is_authentic(&body, session_id, expected_proof.as_deref())
                    {
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

/// 后端 PyInstaller 解压目录：固定在用户可写的 LocalAppData 下，而不是
/// 直接继承可能损坏、被重定向或权限异常的 `%TEMP%`。
///
/// PyInstaller onefile 在启动时要先把运行时解压到临时目录；用户机器上
/// `%TEMP%` 指向已删除的盘、只读目录、被安全软件锁住时，bootloader 只会弹出
/// "Could not create temporary directory!" —— 启动器拿不到可读的原因。
/// 把子进程的 TMP/TEMP 固定到我们自己的目录，至少把这一类环境问题关进笼子。
fn backend_runtime_temp() -> PathBuf {
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        if !local.is_empty() {
            return PathBuf::from(local)
                .join("LunarShroud")
                .join("runtime-temp");
        }
    }
    std::env::temp_dir().join("LunarShroud-runtime-temp")
}

/// 清理上次被强制终止后留下的 `_MEI*` 解压目录。
///
/// 只删一小时以前的：正常退出的 PyInstaller 自己会删；强制终止/崩溃会留下，
/// 多跑几次就可能在用户 Temp 里堆积几个 GB（实测一次开发测试留下 167 个、
/// 2.2 GB）。留一小时是为了不误删另一个仍在启动/运行的实例正在用的目录。
fn cleanup_stale_pyinstaller_dirs(dir: &Path) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    let now = std::time::SystemTime::now();
    for entry in entries.flatten() {
        if !entry.file_name().to_string_lossy().starts_with("_MEI") {
            continue;
        }
        let old = entry
            .metadata()
            .and_then(|meta| meta.modified())
            .ok()
            .and_then(|modified| now.duration_since(modified).ok())
            .is_some_and(|age| age.as_secs() > 3600);
        if old {
            let _ = std::fs::remove_dir_all(entry.path());
        }
    }
}

fn prepare_backend_runtime_temp() -> Result<PathBuf, String> {
    let mut candidates = vec![backend_runtime_temp()];
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("runtime-temp"));
        }
    }
    candidates.push(std::env::temp_dir().join("LunarShroud-runtime-temp"));
    let mut last_error = String::new();
    for dir in candidates {
        match std::fs::create_dir_all(&dir) {
            Ok(()) => {
                let probe = dir.join(".write-probe");
                match std::fs::write(&probe, b"probe") {
                    Ok(()) => {
                        let _ = std::fs::remove_file(&probe);
                        cleanup_stale_pyinstaller_dirs(&dir);
                        return Ok(dir);
                    }
                    Err(error) => last_error = format!("{}: {error}", dir.display()),
                }
            }
            Err(error) => last_error = format!("{}: {error}", dir.display()),
        }
    }
    Err(format!("no writable backend temp directory ({last_error})"))
}

fn open_backend_child_log(dir: &Path) -> Option<std::fs::File> {
    let path = dir.join("backend-child.log");
    if std::fs::metadata(&path)
        .map(|meta| meta.len() > 1024 * 1024)
        .unwrap_or(false)
    {
        let _ = std::fs::remove_file(&path);
    }
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .ok()
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
        let runtime_temp = prepare_backend_runtime_temp().unwrap_or_else(|error| {
            append_launcher_log(&format!(
                "warning: dedicated backend temp unavailable, falling back to %TEMP%: {error}"
            ));
            std::env::temp_dir()
        });
        append_launcher_log(&format!(
            "starting backend exe={} temp={}",
            backend_exe.display(),
            runtime_temp.display()
        ));
        let mut cmd = Command::new(&backend_exe);
        cmd.current_dir(&backend_dir)
            .env("LUNARSHROUD_SESSION_ID", session_id)
            .env("LUNARSHROUD_SESSION_TOKEN", session_token)
            // 子进程的 TMP/TEMP 固定到可写目录（PyInstaller onefile 时代用于
            // 解压；onedir 不再需要，但第三方探测/临时文件仍可能使用）。
            .env("TMP", &runtime_temp)
            .env("TEMP", &runtime_temp)
            // stdout 丢弃；stderr 落盘到 exe 同目录。后端自己的日志依赖
            // APPDATA 可写，受限环境里可能一条都写不出来；uvicorn 的
            // "Application startup failed" traceback 是唯一可读的死因，
            // 必须留下。文件按 1 MB 轮转。
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(match open_backend_child_log(&backend_dir) {
                Some(file) => Stdio::from(file),
                None => Stdio::null(),
            });
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

/// 启动器日志的候选路径。首选 %APPDATA%，失败时退到 exe 同目录的 logs/
/// 再退到系统临时目录，保证"受限环境里也能留下诊断证据"。
fn launcher_log_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(appdata) = std::env::var_os("APPDATA") {
        candidates.push(
            PathBuf::from(appdata)
                .join("LunarShroud")
                .join("logs")
                .join("launcher.log"),
        );
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("logs").join("launcher.log"));
        }
    }
    candidates.push(
        std::env::temp_dir()
            .join("LunarShroud")
            .join("logs")
            .join("launcher.log"),
    );
    candidates
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

const LAUNCHER_LOG_MAX_BYTES: u64 = 1024 * 1024;

fn append_launcher_log_at(path: &Path, timestamp_secs: u64, message: &str) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    // 简单轮转（审计 R7）：超过上限就把现文件挪成 .1（覆盖上一份）再开新文件。
    // 启动器日志一天最多几十行，单备份足够；不为此引入日志库。
    if std::fs::metadata(path)
        .map(|meta| meta.len() >= LAUNCHER_LOG_MAX_BYTES)
        .unwrap_or(false)
    {
        let _ = std::fs::rename(path, path.with_extension("log.1"));
    }
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    writeln!(file, "[{timestamp_secs}] {message}")
}

fn append_launcher_log(message: &str) {
    let timestamp = now_secs();
    for path in launcher_log_candidates() {
        if append_launcher_log_at(&path, timestamp, message).is_ok() {
            return;
        }
    }
}

/// 启动引导日志：写在 exe 同目录，**不依赖 APPDATA**。
///
/// 用户机器上出现过 `%APPDATA%` 被沙箱/受限令牌重定向/拒绝写的情况：
/// 启动器和后端都写不了日志，界面上只有"后端启动失败"。把环境变量和
/// 版本先落到 exe 旁边，下一次至少能判断它是被谁、以什么环境拉起的。
fn write_boot_log(exe_dir: &Path) {
    let message = format!(
        "launcher boot version={} pid={} exe={:?} APPDATA={:?} LOCALAPPDATA={:?} TEMP={:?} TMP={:?} USERPROFILE={:?} USERNAME={:?}",
        env!("CARGO_PKG_VERSION"),
        std::process::id(),
        std::env::current_exe().ok(),
        std::env::var_os("APPDATA"),
        std::env::var_os("LOCALAPPDATA"),
        std::env::var_os("TEMP"),
        std::env::var_os("TMP"),
        std::env::var_os("USERPROFILE"),
        std::env::var_os("USERNAME"),
    );
    let _ = append_launcher_log_at(&exe_dir.join("launcher-boot.log"), now_secs(), &message);
}

#[tauri::command]
fn get_backend_session(
    supervisor: tauri::State<Arc<BackendSupervisor>>,
) -> Option<BackendSessionInfo> {
    // 只暴露**已通过 HMAC 就绪校验**的当前代。重启窗口里返回 None：前端
    // 因此不会向一个尚未确认身份的端口发送令牌（api.js 在 Tauri 模式下
    // 不再回落到硬编码端口）。
    supervisor
        .current_session()
        .map(|session| BackendSessionInfo {
            url: format!("http://127.0.0.1:{}", supervisor.port),
            token: session.token,
        })
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
        append_launcher_log_at, backend_readiness_is_authentic, backend_runtime_temp,
        external_url_is_allowed, parse_backend_port, random_session_secret, readiness_proof,
        ready_probe_url, supervision_backoff, system_tool, BackendSupervisor,
        LAUNCHER_LOG_MAX_BYTES,
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
    fn backend_runtime_temp_is_a_private_local_directory() {
        let path = backend_runtime_temp();
        assert!(path.ends_with(std::path::Path::new("LunarShroud").join("runtime-temp")));
    }

    #[test]
    fn external_urls_are_limited_to_http_and_https() {
        assert!(external_url_is_allowed("https"));
        assert!(external_url_is_allowed("http"));
        // file:// 能读本机文件；javascript:/data: 更不该交给 ShellExecuteW。
        for scheme in ["file", "javascript", "data", "ms-settings", "vbscript", ""] {
            assert!(!external_url_is_allowed(scheme), "{scheme}");
        }
    }

    #[test]
    fn readiness_proof_is_hmac_sha256_over_context_and_nonce() {
        // 独立算出的向量（Python: hmac.new(b"test-token",
        // b"lunarshroud-ready-v1:abc", hashlib.sha256).hexdigest()）。
        // 两边算法一旦漂移（前缀、编码、大小写），打包版会永远"后端未就绪"。
        assert_eq!(
            readiness_proof("test-token", "abc"),
            "951d77889ee123b9d548194dbe393bf4a6900de3730820ce37a0e7f67486ee02"
        );
        assert_ne!(
            readiness_proof("test-token", "abc"),
            readiness_proof("test-token", "abd"),
            "nonce 必须真正参与摘要"
        );
        assert_ne!(
            readiness_proof("test-token", "abc"),
            readiness_proof("other-token", "abc"),
            "令牌必须真正参与摘要"
        );
    }

    #[test]
    fn forged_peer_echoing_the_session_id_is_rejected() {
        // 占住端口的进程能从请求 URL 里读到 session_id 并原样回显 —— 旧实现
        // 把这当成"身份已确认"。现在没有正确 proof 一律不认。
        let echoed = r#"{"status":"ready","session_id":"fresh-session","pid":123}"#;
        assert!(!backend_readiness_is_authentic(
            echoed,
            "fresh-session",
            Some("951d77889ee123b9d548194dbe393bf4a6900de3730820ce37a0e7f67486ee02")
        ));

        let wrong_session = format!(
            r#"{{"status":"ready","session_id":"other","proof":"{}"}}"#,
            readiness_proof("test-token", "abc")
        );
        assert!(!backend_readiness_is_authentic(
            &wrong_session,
            "fresh-session",
            Some(&readiness_proof("test-token", "abc"))
        ));

        // 正确答案必须通过；开发模式（无令牌）退化为只核对会话号。
        let authentic = format!(
            r#"{{"status":"ready","session_id":"fresh-session","proof":"{}"}}"#,
            readiness_proof("test-token", "abc")
        );
        assert!(backend_readiness_is_authentic(
            &authentic,
            "fresh-session",
            Some(&readiness_proof("test-token", "abc"))
        ));
        assert!(backend_readiness_is_authentic(
            echoed,
            "fresh-session",
            None
        ));
        assert!(!backend_readiness_is_authentic(
            "not-json",
            "fresh-session",
            None
        ));
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
    fn launcher_log_rotates_when_it_exceeds_the_limit() {
        let dir =
            std::env::temp_dir().join(format!("lunarshroud-log-rotate-{}", std::process::id()));
        let path = dir.join("logs").join("launcher.log");
        let _ = std::fs::remove_dir_all(&dir);

        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, vec![b'x'; LAUNCHER_LOG_MAX_BYTES as usize]).unwrap();

        append_launcher_log_at(&path, 2000, "after rotation").unwrap();

        let backup = path.with_extension("log.1");
        assert!(backup.exists(), "超限后未轮转出 .1 备份");
        let contents = std::fs::read_to_string(&path).unwrap();
        assert!(contents.contains("[2000] after rotation"));
        assert!(!contents.contains('x'), "新文件不该带上旧内容");

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn generation_registration_refuses_after_shutdown() {
        let supervisor = BackendSupervisor::new(17520);
        supervisor.set_session("t".into());
        supervisor.request_shutdown();
        assert!(!supervisor.register_generation(222, None));
        // 关闭时立即丢弃会话：前端不能再拿到令牌。
        assert!(supervisor.current_session().is_none());
        // 重复关闭请求必须安全（句柄已取出为空）
        supervisor.request_shutdown();
    }

    #[test]
    fn session_is_not_exposed_until_a_generation_is_verified() {
        let supervisor = BackendSupervisor::new(17520);
        assert!(supervisor.current_session().is_none());
        supervisor.set_session("token-1".into());
        assert_eq!(
            supervisor
                .current_session()
                .expect("verified session")
                .token,
            "token-1"
        );
        // 后端退出 → 重启窗口：必须先清空，前端才会停止携带旧令牌。
        supervisor.clear_session();
        assert!(supervisor.current_session().is_none());
        // 新一代用新令牌（旧令牌不再出现）。
        supervisor.set_session("token-2".into());
        assert_eq!(supervisor.current_session().unwrap().token, "token-2");
    }

    #[test]
    fn readiness_probe_carries_a_nonce_but_never_the_token() {
        // 就绪探测打的是不需要令牌的端点，凭 nonce 的 HMAC 应答证明对端身份。
        // 曾经的做法是带着令牌去探 /api/system/health —— 那等于在确认"对面是
        // 谁"之前就把钥匙递出去，任何占住该端口的本地进程都能收到。
        let url = ready_probe_url(17520, "abc123", "deadbeef");
        assert_eq!(
            url,
            "http://127.0.0.1:17520/api/system/ready?session=abc123&nonce=deadbeef"
        );
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
        assert_eq!(
            system_tool("taskkill.exe").to_string_lossy(),
            "taskkill.exe"
        );
    }
}
