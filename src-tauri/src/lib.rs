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
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, WebviewWindowBuilder};

// Windows 平台：阻止子进程弹出控制台窗口
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

const BACKEND_READINESS_TIMEOUT: Duration = Duration::from_secs(30);
/// 就绪响应的读取上限。正常响应只有几百字节；上限只用于挡住占端口的本地
/// 进程用超大响应体拖垮外壳。
const MAX_READINESS_BODY_BYTES: u64 = 8 * 1024;

/// 单实例互斥锁。返回的句柄必须活到进程结束（调用方 mem::forget）。
///
/// 没有它时，重复双击会再起一套外壳 + 后端：第二个后端抢不到 17520，uvicorn
/// 以 exit 3 退出，用户看到"后端进程启动失败"——而第一个实例（窗口可能被其他
/// 窗口挡住/最小化）其实一直好好的。实测用户就是这么连续点出来的错误。
/// 单实例检查的三种结果。`Unavailable` 与 `AlreadyRunning` 必须分开：
/// 前者（CreateMutexW 失败）如果被当成"已在运行"，应用会带着一个错误的
/// 诊断永久无法启动；注释里承诺的"宁可放行"也就没有兑现。
enum SingleInstance {
    Acquired(usize),
    AlreadyRunning,
    Unavailable,
}

#[cfg(windows)]
fn acquire_single_instance() -> SingleInstance {
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
        return SingleInstance::Unavailable;
    }
    if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
        unsafe {
            CloseHandle(handle);
        }
        return SingleInstance::AlreadyRunning;
    }
    SingleInstance::Acquired(handle as usize)
}

/// 二次启动时把已有窗口拉到前台；找不到窗口才弹提示。
#[cfg(windows)]
fn focus_existing_window() -> bool {
    use windows_sys::Win32::Foundation::{BOOL, HWND, LPARAM};
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetWindowTextW, SetForegroundWindow, ShowWindow, SW_RESTORE,
    };

    // FindWindowW proved unreliable in this environment even for a visible
    // Tauri window (returns 0 while EnumWindows sees it). Enumerate top-level
    // windows and match the stable product title instead.
    unsafe extern "system" fn find_main(hwnd: HWND, lparam: LPARAM) -> BOOL {
        let mut buffer = [0u16; 128];
        let length = GetWindowTextW(hwnd, buffer.as_mut_ptr(), buffer.len() as i32);
        if length > 0 {
            let title = String::from_utf16_lossy(&buffer[..length as usize]);
            if title == "LunarShroud" {
                *(lparam as *mut HWND) = hwnd;
                return 0; // stop enumeration
            }
        }
        1
    }

    let mut found: HWND = std::ptr::null_mut();
    unsafe {
        EnumWindows(Some(find_main), &mut found as *mut HWND as LPARAM);
    }
    if found.is_null() {
        append_launcher_log("second instance: existing main window not found");
        return false;
    }
    unsafe {
        ShowWindow(found, SW_RESTORE);
        SetForegroundWindow(found);
    }
    append_launcher_log("second instance: existing main window restored");
    true
}

#[cfg(not(windows))]
fn focus_existing_window() -> bool {
    false
}

#[cfg(not(windows))]
fn acquire_single_instance() -> SingleInstance {
    SingleInstance::Acquired(1)
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
/// Desktop lifecycle state shared by the tray menu and the close handler.
///
/// `close_to_tray` is opt-in through the backend config / Settings page: an
/// existing user who closes the window still exits exactly as before unless
/// they enable it. `tray_ready` is false when Tauri could not create the tray,
/// so a close request falls back to the normal shutdown path.
struct TrayState {
    close_to_tray: AtomicBool,
    tray_ready: AtomicBool,
    quitting: AtomicBool,
    /// 首次隐藏到托盘时提示一次"怎么真正退出"；之后不再重复。
    tray_hint_shown: AtomicBool,
}

impl TrayState {
    fn new(close_to_tray: bool) -> Self {
        Self {
            close_to_tray: AtomicBool::new(close_to_tray),
            tray_ready: AtomicBool::new(false),
            quitting: AtomicBool::new(false),
            tray_hint_shown: AtomicBool::new(false),
        }
    }
}

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
    /// 返回 false 表示关闭请求已经赢得竞争，令牌不得再发布给前端。
    fn set_session(&self, token: String) -> bool {
        let mut guard = self.session.lock().unwrap();
        if self.is_shutdown() {
            return false;
        }
        *guard = Some(ActiveSession { token });
        true
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
            // 关闭 Job Object 会发起终止，但 CloseHandle 本身不等待进程退出。
            // 主窗口关闭后 Tauri 很快结束进程，监管线程可能来不及写
            // "backend exited during shutdown"；这里做一次有界等待并自行记录，
            // 保证关闭证据一定落盘（同时也确保退出前端口已释放）。
            let exited = pid.is_some_and(|pid| wait_for_process_exit(pid, Duration::from_secs(2)));
            append_launcher_log(&format!(
                "backend exited during shutdown: job closed exited={exited}"
            ));
        } else if let Some(pid) = pid {
            // Job Object 附加失败时的兜底路径
            kill_process_tree(pid);
            let exited = wait_for_process_exit(pid, Duration::from_secs(2));
            append_launcher_log(&format!(
                "backend exited during shutdown: tree kill exited={exited}"
            ));
        } else {
            append_launcher_log("backend exited during shutdown: no live generation");
        }
        append_launcher_log("launcher shutdown requested");
    }
}

/// 等待某个 pid 退出，最多 ``timeout``；返回是否在超时前观察到退出。
#[cfg(windows)]
fn wait_for_process_exit(pid: u32, timeout: Duration) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0};
    use windows_sys::Win32::System::Threading::{OpenProcess, WaitForSingleObject};

    /// `SYNCHRONIZE` 是 `PROCESS_ACCESS_RIGHTS` 的位，windows-sys 0.59 把它
    /// 放在 FileSystem 模块下；这里直接写值，避免为一个常量引入额外 feature。
    const SYNCHRONIZE: u32 = 0x0010_0000;

    let handle = unsafe { OpenProcess(SYNCHRONIZE, 0, pid) };
    if handle.is_null() {
        return false;
    }
    let millis = timeout.as_millis().min(u32::MAX as u128) as u32;
    let result = unsafe { WaitForSingleObject(handle, millis) };
    unsafe {
        CloseHandle(handle);
    }
    result == WAIT_OBJECT_0
}

#[cfg(not(windows))]
fn wait_for_process_exit(_pid: u32, _timeout: Duration) -> bool {
    false
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
        SingleInstance::Acquired(handle) => handle,
        SingleInstance::AlreadyRunning => {
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
        SingleInstance::Unavailable => {
            // 与注释承诺一致：mutex API 本身失败时放行，但留下证据。
            append_launcher_log(
                "warning: single-instance mutex unavailable; continuing without the guard",
            );
            0
        }
    };
    // 独立于 APPDATA 的引导记录已在最前面写；这里不再重复。
    // 与后端读取同一份持久化配置，保证用户修改 launcher_port 后，
    // Tauri 仍能在重启时探测到正确的 sidecar 端口。
    let port = configured_backend_port();
    let close_to_tray = configured_close_to_tray();
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
                 受限环境另有：应用目录下的 launcher-boot.log、backend\\backend-child.log\n\
                 或 runtime-temp\\backend-child.log"
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
        .manage(TrayState::new(close_to_tray))
        .invoke_handler(tauri::generate_handler![
            get_backend_session,
            set_close_to_tray,
            quit_app,
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
                    // 关闭 WebView2 的原生拖放接管。
                    //
                    // Tauri 2 默认 `drag_drop_handler_enabled = true`：wry 会注册
                    // IDropTarget，把整个 webview 变成原生拖放目标，于是 HTML5 的
                    // dragenter/dragover/drop **根本不会派发给页面**。表现就是
                    // "把文件拖进聊天窗口毫无反应"—— 而源码运行通道（浏览器直接
                    // 打开 127.0.0.1）一切正常，所以这类缺陷只有打包态才暴露。
                    // 页面自己用 File API 读文件，不需要原生通道。
                    .disable_drag_drop_handler()
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
            let window_builder = match webview2_data_dir(window_log_dir.as_deref()) {
                Some(dir) => window_builder.data_directory(dir),
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
                    // 窗口建不出来时进程很快退出；Job Object 通常能回收后端，
                    // 但 Job 附加失败时没有别的路径会停它。显式走一次关闭流程。
                    if let Some(supervisor) = app.try_state::<Arc<BackendSupervisor>>() {
                        supervisor.request_shutdown();
                    }
                    show_error_box(
                        "LunarShroud 启动错误",
                        &format!(
                            "主窗口创建失败：{error}\n\n\
                             常见原因：WebView2 Runtime 未安装/损坏，或可写的数据目录 \
                             无法创建。\n\
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

            match create_tray(app) {
                Some(tray) => {
                    app.manage(tray);
                    if let Some(state) = app.try_state::<TrayState>() {
                        state.tray_ready.store(true, Ordering::Relaxed);
                    }
                }
                None => {
                    append_launcher_log(
                        "warning: tray unavailable; close-to-tray disabled for this run",
                    );
                }
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let state = window.app_handle().try_state::<TrayState>();
                let (tray_ready, close_to_tray, quitting) = state
                    .as_ref()
                    .map(|state| {
                        (
                            state.tray_ready.load(Ordering::Relaxed),
                            state.close_to_tray.load(Ordering::Relaxed),
                            state.quitting.load(Ordering::Relaxed),
                        )
                    })
                    .unwrap_or((false, false, false));
                append_launcher_log(&format!(
                    "close requested: tray_ready={tray_ready} close_to_tray={close_to_tray} quitting={quitting}"
                ));
                let hide_to_tray = tray_ready && close_to_tray && !quitting;
                if hide_to_tray {
                    api.prevent_close();
                    let _ = window.hide();
                    // 第一次隐藏时把"怎么真正退出"写进 boot log，并让托盘 tooltip
                    // 直接说出来。不引入通知插件（那是一个新的权限面），
                    // 但也不能什么都不做 —— 2026-11-03 实测：点 X 后窗口消失、
                    // 进程还在，界面上没有任何退出入口（唯一入口是 Ctrl+K），
                    // 用户能得出的唯一结论就是"关不掉"。
                    let first_hide = state
                        .as_ref()
                        .map(|s| !s.tray_hint_shown.swap(true, Ordering::Relaxed))
                        .unwrap_or(true);
                    if first_hide {
                        append_launcher_log(
                            "main window hidden to tray (first time); use Settings -> Backups \
                             -> Application lifecycle -> Quit, or right-click the tray icon",
                        );
                        if let Some(tray) = window.app_handle().tray_by_id("lunarshroud-tray") {
                            let _ = tray.set_tooltip(Some(
                                "LunarShroud — running in the tray. \
                                 Right-click to quit, or use Settings → Application lifecycle.",
                            ));
                        }
                    } else {
                        append_launcher_log("main window hidden to tray");
                    }
                    return;
                }
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
    let mut consecutive_failures: u32 = 0;
    let mut generation_started = Instant::now();
    loop {
        let exit = child.wait();
        // 这一代已经死了：先让前端拿不到这一代的令牌，再决定是否重启。
        // 前端在重连窗口里只会拿到 None，因此不会把旧令牌发给抢先占端口的进程；
        // 即使它仍缓存着旧令牌，重启也会换发新的，旧值不可用于新一代后端。
        supervisor.clear_session();
        supervisor.take_and_close_job();
        if supervisor.is_shutdown() {
            append_launcher_log(&format!("backend exited during shutdown: {exit:?}"));
            return;
        }
        // 重启风暴闸门：后端"就绪后很快又退出"（schema/驱动/重连客户端触发
        // 的崩溃）不能让监管线程无延迟地无限重生。只有稳定运行超过 60s 才把
        // 失败计数归零；否则逐次退避到 30s 封顶。
        let uptime = generation_started.elapsed();
        consecutive_failures = next_generation_failures(consecutive_failures, uptime);
        append_launcher_log(&format!(
            "backend exited unexpectedly: {exit:?}; uptime={}s failures={consecutive_failures}; restarting",
            uptime.as_secs()
        ));
        if consecutive_failures > 0 {
            std::thread::sleep(supervision_backoff(consecutive_failures));
        }
        match spawn_ready_generation(&supervisor) {
            Some(next_child) => {
                child = next_child;
                generation_started = Instant::now();
            }
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

/// 一代后端退出后更新"连续快速失败"计数。
/// 只有稳定运行超过 60s 才归零，避免"就绪后立刻崩溃"的进程被无延迟地
/// 无限重生（每次都会占用 CPU/磁盘并把 UI 刷成不停闪烁的会话）。
fn next_generation_failures(previous: u32, uptime: Duration) -> u32 {
    if uptime < Duration::from_secs(60) {
        previous.saturating_add(1)
    } else {
        0
    }
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
        // 占住端口的本地进程可以回 302 把探测引向任意 URL；探测只需要看
        // 本端口的应答，不跟随重定向。
        .redirect(reqwest::redirect::Policy::none())
        .build();
    let Ok(client) = client else {
        append_launcher_log("failed to create backend readiness client");
        return false;
    };

    let start = Instant::now();
    while start.elapsed() < timeout {
        if let Ok(Some(status)) = child.try_wait() {
            eprintln!("backend exited before readiness: {status}");
            append_launcher_log(&format!("backend exited before readiness: {status}"));
            return false;
        }
        if let Ok(resp) = client.get(&url).send() {
            if resp.status().is_success() {
                // 只读有界长度：端口上的任意本地进程都可以流式返回一个超大
                // 响应体，无界 resp.text() 会让外壳在窗口创建前 OOM。
                use std::io::Read as _;
                let mut body = String::new();
                if resp
                    .take(MAX_READINESS_BODY_BYTES)
                    .read_to_string(&mut body)
                    .is_ok()
                    && backend_readiness_is_authentic(&body, session_id, expected_proof.as_deref())
                {
                    return true;
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
    // 后端用 utf-8-sig 读配置（记事本保存的文件带 BOM）。外壳若不剥 BOM，
    // 同一个文件后端能读、外壳读不到，端口就会分叉。
    let contents = contents.trim_start_matches('\u{feff}');
    let value: serde_json::Value = serde_json::from_str(contents).ok()?;
    let port = value.get("launcher_port")?.as_u64()?;
    (1..=65535).contains(&port).then_some(port as u16)
}

/// Read the opt-in "close window hides to tray" preference.
///
/// Missing or malformed values deliberately mean false: upgrading users keep
/// the historical close-exits behavior.
fn parse_close_to_tray(contents: &str) -> Option<bool> {
    let contents = contents.trim_start_matches('\u{feff}');
    let value: serde_json::Value = serde_json::from_str(contents).ok()?;
    value.get("close_to_tray")?.as_bool()
}

fn read_close_to_tray_from(dir: &Path) -> Option<bool> {
    std::fs::read_to_string(dir.join("config.json"))
        .ok()
        .and_then(|contents| parse_close_to_tray(&contents))
}

/// Same candidate order as `configured_backend_port`, so a user who edits the
/// fallback data directory gets the same value as the backend.
fn configured_close_to_tray() -> bool {
    #[cfg(debug_assertions)]
    {
        if let Ok(exe) = std::env::current_exe() {
            let mut current = exe.parent().map(Path::to_path_buf);
            while let Some(dir) = current {
                if dir.join("backend").join("main.py").is_file() {
                    return read_close_to_tray_from(&dir.join("data")).unwrap_or(false);
                }
                current = dir.parent().map(Path::to_path_buf);
            }
        }
    }
    for dir in data_dir_candidates() {
        if dir_is_writable(&dir) {
            if let Some(value) = read_close_to_tray_from(&dir) {
                return value;
            }
        }
    }
    false
}

/// 能否在目录中创建/写入探针文件（与 backend/config.py 的选择口径一致）。
fn dir_is_writable(path: &Path) -> bool {
    if std::fs::create_dir_all(path).is_err() {
        return false;
    }
    let probe = path.join(format!(".write-probe-{}", std::process::id()));
    match std::fs::write(&probe, b"probe") {
        Ok(()) => {
            let _ = std::fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

/// 后端 `_frozen_data_dir()` 的候选顺序；两侧必须一致，否则端口/配置分叉。
fn data_dir_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(appdata) = std::env::var_os("APPDATA") {
        candidates.push(PathBuf::from(appdata).join("LunarShroud"));
    }
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        candidates.push(PathBuf::from(local).join("LunarShroud"));
    }
    if let Some(home) = std::env::var_os("USERPROFILE") {
        candidates.push(PathBuf::from(home).join("LunarShroud"));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            // 后端的 exe 在 <launcher_dir>\backend\backend.exe，它的
            // `sys.executable.parent/data` 是 <launcher_dir>\backend\data，
            // 不是 <launcher_dir>\data。候选目录必须逐字对齐，否则受限环境里
            // 两侧仍会选到不同的数据目录。
            candidates.push(dir.join("backend").join("data"));
        }
    }
    candidates.push(std::env::temp_dir().join("LunarShroud"));
    candidates
}

fn read_port_from(dir: &Path) -> Option<u16> {
    std::fs::read_to_string(dir.join("config.json"))
        .ok()
        .and_then(|contents| parse_backend_port(&contents))
}

/// 读取与后端**同一份**生效配置里的启动器端口。
///
/// 旧实现只读 `%APPDATA%\LunarShroud\config.json`，而后端在受限环境里会
/// 回退到 `%LOCALAPPDATA%`/`%USERPROFILE%`/`exe\data` 并把配置写在那里。
/// 外壳随后用 `LUNARSHROUD_LAUNCHER_PORT` 覆盖后端读到的端口，于是用户在
/// 回退目录里改的 `launcher_port` 永远不会生效。这里复刻同一套"可写才用"
/// 的候选顺序，并保留 debug 构建从源码树 `data/config.json` 读取的开发语义。
fn configured_backend_port() -> u16 {
    const DEFAULT_PORT: u16 = 17520;

    #[cfg(debug_assertions)]
    {
        // 开发模式：后端是 `python backend/main.py`，DATA_DIR = <repo>/data。
        if let Ok(exe) = std::env::current_exe() {
            let mut current = exe.parent().map(Path::to_path_buf);
            while let Some(dir) = current {
                if dir.join("backend").join("main.py").is_file() {
                    return read_port_from(&dir.join("data")).unwrap_or(DEFAULT_PORT);
                }
                current = dir.parent().map(Path::to_path_buf);
            }
        }
    }

    let preferred =
        std::env::var_os("APPDATA").map(|value| PathBuf::from(value).join("LunarShroud"));
    for dir in data_dir_candidates() {
        if dir_is_writable(&dir) {
            if let Some(port) = read_port_from(&dir) {
                return port;
            }
            // 后端会在这个目录创建/播种配置；若首选 APPDATA 配置存在，
            // 播种内容里的端口就是后端将要使用的端口。
            if let Some(preferred) = &preferred {
                if let Some(port) = read_port_from(preferred) {
                    return port;
                }
            }
            return DEFAULT_PORT;
        }
    }
    preferred
        .as_deref()
        .and_then(read_port_from)
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
        if assigned == 0 {
            // 必须在 CloseHandle 之前取 LastError：CloseHandle 会覆盖它。
            let error = std::io::Error::last_os_error();
            CloseHandle(process);
            CloseHandle(job);
            return Err(format!("AssignProcessToJobObject failed: {}", error));
        }
        CloseHandle(process);
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

/// WebView2 用户数据目录：优先 exe 同目录（受限令牌下通常只有这里可写），
/// 不可写时退回 `%LOCALAPPDATA%\LunarShroud\webview2-data`。
///
/// 旧实现无条件使用 exe 同目录：只读安装位置（Program Files/只读 U 盘）下
/// 窗口永远建不出来，而默认剖面本来是可用的。探测失败时最后返回 None，
/// 让 Tauri 使用它自己的默认目录。
fn webview2_data_dir(exe_dir: Option<&Path>) -> Option<PathBuf> {
    if let Some(dir) = exe_dir {
        let candidate = dir.join("webview2-data");
        if dir_is_writable(&candidate) {
            return Some(candidate);
        }
    }
    let local = std::env::var_os("LOCALAPPDATA")?;
    let candidate = PathBuf::from(local)
        .join("LunarShroud")
        .join("webview2-data");
    dir_is_writable(&candidate).then_some(candidate)
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
            // 版本号的单一来源是 tauri.conf.json（编译期由 CARGO_PKG_VERSION
            // 带进来）。后端不自己猜版本，只转发给设置页的「开发者」分类。
            .env("LUNARSHROUD_APP_VERSION", env!("CARGO_PKG_VERSION"))
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
            .stderr(
                match open_backend_child_log(&backend_dir)
                    .or_else(|| open_backend_child_log(&runtime_temp))
                {
                    Some(file) => Stdio::from(file),
                    None => {
                        append_launcher_log(
                            "warning: backend child log unavailable; stderr will be discarded",
                        );
                        Stdio::null()
                    }
                },
            );
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
        let Some(python_exe) = find_python() else {
            return Err(
                "找不到 Python 解释器（where.exe 不可用或 PATH 中没有 python）".to_string(),
            );
        };

        let main_py = backend_dir.join("main.py");
        if !main_py.exists() {
            return Err("找不到 backend/main.py 也找不到 backend.exe\n\n".to_string());
        }

        let mut cmd = Command::new(&python_exe);
        cmd.arg(&main_py)
            .current_dir(&backend_dir)
            .env("LUNARSHROUD_SESSION_ID", session_id)
            .env("LUNARSHROUD_SESSION_TOKEN", session_token)
            .env("LUNARSHROUD_APP_VERSION", env!("CARGO_PKG_VERSION"))
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
fn find_python() -> Option<String> {
    // 优先级：python > python3 > py。绝不在找不到时返回裸名 "python" ——
    // 那会让 CreateProcess 按应用目录/CWD 搜索，重新打开可写目录劫持。
    let where_exe = system_tool("where.exe")?;
    for name in &["python", "python3", "py"] {
        let mut cmd = Command::new(&where_exe);
        cmd.arg(name);
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);
        if let Ok(output) = cmd.output() {
            if output.status.success() {
                let stdout = String::from_utf8_lossy(&output.stdout);
                if let Some(first_line) = stdout.lines().next() {
                    let candidate = first_line.trim();
                    if !candidate.is_empty() {
                        return Some(candidate.to_string());
                    }
                }
            }
        }
    }
    None
}

/// System32 里某个工具的绝对路径；拿不到系统目录或工具不存在时返回 `None`。
///
/// 系统工具**必须**用绝对路径调用。裸名会走 CreateProcess 的搜索序，而
/// "应用程序所在目录"与"当前目录"都排在 System32 之前 —— 外壳的 CWD 来自快捷
/// 方式的 WorkingDirectory（Publish-PortableRuntime.ps1:88 把它设成用户可写的
/// `<Dest>`），实测在那里植入 taskkill.exe 后，跑的是植入副本。
///
/// 目录由 `GetSystemDirectoryW` 给出，**不读 `SystemRoot`/`windir` 环境变量**：
/// 环境可以被调用方/计划任务污染，指向一个攻击者控制的 "System32"。
/// 拿不到目录时返回 `None`，调用方必须禁用该工具（Job Object 仍是主路径；
/// psutil 兜底在后端）。退回裸名等于把可写目录劫持重新引进来，AGENTS §2.1
/// 明确禁止。
fn system_tool(name: &str) -> Option<PathBuf> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW;

        let mut buffer = [0u16; 260];
        let length = unsafe { GetSystemDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32) };
        if length == 0 || length as usize >= buffer.len() {
            return None;
        }
        let system32 = PathBuf::from(String::from_utf16_lossy(&buffer[..length as usize]));
        let candidate = system32.join(name);
        candidate.is_file().then_some(candidate)
    }
    #[cfg(not(windows))]
    {
        let _ = name;
        None
    }
}

/// 杀掉进程树（包括所有子进程）
fn kill_process_tree(pid: u32) {
    // Windows: taskkill /F /T /PID（绝对路径，见 system_tool）。
    // 解析不到时绝不退回裸名：Job Object 是主路径，这里只是兜底。
    let Some(tool) = system_tool("taskkill.exe") else {
        append_launcher_log(&format!(
            "warning: taskkill.exe unavailable (SystemRoot missing); cannot terminate process tree pid={pid}"
        ));
        return;
    };
    let mut cmd = Command::new(tool);
    cmd.args(["/F", "/T", "/PID", &pid.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    match cmd.status() {
        Ok(status) if status.success() => {}
        Ok(status) => append_launcher_log(&format!(
            "warning: taskkill exited with {status} for pid={pid}"
        )),
        Err(error) => append_launcher_log(&format!(
            "warning: taskkill could not run for pid={pid}: {error}"
        )),
    }
}

/// 启动器日志的候选路径。首选 %APPDATA%，失败时退到 exe 同目录的 logs/
/// 再退到系统临时目录，保证"受限环境里也能留下诊断证据"。
///
/// 只在非 test 构建里被 `append_launcher_log` 使用：test 构建走
/// `test_launcher_log_path()`，绝不写用户目录。没有这条 cfg 时，
/// test 构建里本函数无人调用，clippy 的 `dead_code` 在 `-D warnings` 下会
/// 直接报错（CI 红的原因之一）。
#[cfg(not(test))]
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

/// 串行化日志写入/轮转：监管线程与 setup 线程会并发写同一个文件，轮转时
/// 的 rename 与 append 必须互斥，否则可能丢行或把新行写进已改名的文件。
static LAUNCHER_LOG_LOCK: Mutex<()> = Mutex::new(());

fn append_launcher_log_at(path: &Path, timestamp_secs: u64, message: &str) -> std::io::Result<()> {
    let _guard = LAUNCHER_LOG_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    // 简单轮转：超过上限就把现文件挪成 .1（覆盖上一份）再开新文件。
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

/// 测试进程专用的启动器日志路径：放在测试二进制旁边（Cargo 刚写过它，保证可写），
/// 不依赖 %APPDATA% 也不依赖继承的 %TEMP%。整个测试进程共用一份，由 `OnceLock`
/// 保证只算一次；测试之间并行写同一份日志不影响任何断言（没人断言它的内容）。
#[cfg(test)]
fn test_launcher_log_path() -> PathBuf {
    static ONCE: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();
    ONCE.get_or_init(|| {
        let base = std::env::current_exe()
            .expect("test executable path")
            .parent()
            .expect("test executable parent")
            .to_path_buf();
        base.join(format!(
            "lunarshroud-test-launcher-log-{}",
            std::process::id()
        ))
        .join("launcher.log")
    })
    .clone()
}

/// 写一条启动器日志。
///
/// 两个构建形态各有一份**完整实现**，用 `#[cfg]` 在函数级别二选一。
/// 不写成"一个函数 + 内部 `#[cfg]` 语句块"的原因：test 构建下那块带
/// `return;`，而它之后的生产代码被 cfg 掉，`return` 就成了函数最后一条语句，
/// clippy 的 `needless_return` 在 `-D warnings` 下直接报错 —— CI 正是这么红的
/// （2026-11-04，两次），而本地 `cargo test` 看不出来，因为它不跑 clippy。
#[cfg(test)]
fn append_launcher_log(message: &str) {
    let timestamp = now_secs();
    // 测试绝不能写真实 %APPDATA%。`request_shutdown()` 会记关闭证据，
    // 而候选路径的首选就是 `%APPDATA%\LunarShroud\logs\launcher.log`：
    // 一是污染用户目录，二是在受限令牌/沙箱下那个位置常常不可写，
    // 于是 `cargo test` 在不同机器上结果不一致（AGENTS §6.5）。
    let path = test_launcher_log_path();
    if let Err(error) = append_launcher_log_at(&path, timestamp, message) {
        eprintln!(
            "warning: test launcher log {} is not writable: {error}",
            path.display()
        );
    }
}

#[cfg(not(test))]
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

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn quit_from_tray(app: &AppHandle) {
    if let Some(state) = app.try_state::<TrayState>() {
        state.quitting.store(true, Ordering::Relaxed);
    }
    if let Some(supervisor) = app.try_state::<Arc<BackendSupervisor>>() {
        supervisor.request_shutdown();
    }
    app.exit(0);
}

/// Build the tray menu. A missing icon or menu must not prevent the app from
/// starting; the caller marks `tray_ready=false` and close falls back to the
/// normal shutdown path.
fn create_tray(app: &mut tauri::App) -> Option<tauri::tray::TrayIcon> {
    let Some(icon) = app.default_window_icon().cloned() else {
        append_launcher_log("warning: tray icon missing; tray unavailable");
        return None;
    };
    let show = match MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>) {
        Ok(item) => item,
        Err(error) => {
            append_launcher_log(&format!("warning: tray show menu failed: {error}"));
            return None;
        }
    };
    let quit = match MenuItem::with_id(app, "quit", "退出 LunarShroud", true, None::<&str>) {
        Ok(item) => item,
        Err(error) => {
            append_launcher_log(&format!("warning: tray quit menu failed: {error}"));
            return None;
        }
    };
    let menu = match Menu::with_items(app, &[&show, &quit]) {
        Ok(menu) => menu,
        Err(error) => {
            append_launcher_log(&format!("warning: tray menu build failed: {error}"));
            return None;
        }
    };
    match TrayIconBuilder::with_id("lunarshroud-tray")
        .icon(icon)
        .tooltip("LunarShroud")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main_window(app),
            "quit" => quit_from_tray(app),
            _ => {}
        })
        .build(app)
    {
        Ok(tray) => {
            append_launcher_log("tray created");
            Some(tray)
        }
        Err(error) => {
            append_launcher_log(&format!("warning: tray creation failed: {error}"));
            None
        }
    }
}

/// Tauri command: enable/disable hide-on-close immediately (the next launch
/// reads the persisted backend config). Returns the effective value.
#[tauri::command]
fn set_close_to_tray(state: tauri::State<TrayState>, enabled: bool) -> bool {
    if !state.tray_ready.load(Ordering::Relaxed) {
        return false;
    }
    state.close_to_tray.store(enabled, Ordering::Relaxed);
    enabled
}

/// Tauri command: explicit quit, also used by the command palette. Needed
/// because with close-to-tray enabled the window close button only hides.
#[tauri::command]
fn quit_app(app: AppHandle) {
    quit_from_tray(&app);
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
        dir_is_writable, external_url_is_allowed, next_generation_failures, parse_backend_port,
        parse_close_to_tray, random_session_secret, readiness_proof, ready_probe_url,
        supervision_backoff, system_tool, wait_for_process_exit, webview2_data_dir,
        BackendSupervisor, LAUNCHER_LOG_MAX_BYTES,
    };
    use std::path::Path;
    use std::time::Duration;

    /// 测试专用目录：放在测试二进制旁边（Cargo 刚写过它，保证可写），
    /// 不依赖继承的 %TEMP% —— 受限令牌/沙箱下 TEMP 可能不可写，测试不能
    /// 因此变成"作者机器 20 passed、别处 16/20"的门禁噪声。
    struct TestDir(std::path::PathBuf);

    impl TestDir {
        fn new(label: &str) -> Self {
            let base = std::env::current_exe()
                .expect("test executable path")
                .parent()
                .expect("test executable parent")
                .to_path_buf();
            let dir = base.join(format!("lunarshroud-test-{label}-{}", std::process::id()));
            if dir.exists() {
                std::fs::remove_dir_all(&dir).expect("remove stale test dir");
            }
            std::fs::create_dir_all(&dir).expect("create test dir next to the test binary");
            Self(dir)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TestDir {
        fn drop(&mut self) {
            // 不用 `let _ =`：清理失败要留下可见记录，便于发现泄漏残留。
            if let Err(error) = std::fs::remove_dir_all(&self.0) {
                eprintln!(
                    "warning: could not remove test dir {}: {error}",
                    self.0.display()
                );
            }
        }
    }

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
    fn parses_a_config_file_saved_with_a_utf8_bom() {
        // 记事本"UTF-8"默认带 BOM；后端用 utf-8-sig 能读，外壳也必须能读。
        assert_eq!(
            parse_backend_port("\u{feff}{\"launcher_port\":17620}"),
            Some(17620)
        );
    }

    #[test]
    fn close_to_tray_is_opt_in_and_rejects_bad_values() {
        assert_eq!(parse_close_to_tray(r#"{"close_to_tray":true}"#), Some(true));
        assert_eq!(
            parse_close_to_tray(r#"{"close_to_tray":false}"#),
            Some(false)
        );
        assert_eq!(
            parse_close_to_tray("\u{feff}{\"close_to_tray\":true}"),
            Some(true)
        );
        assert_eq!(parse_close_to_tray(r#"{"theme":"dark"}"#), None);
        assert_eq!(parse_close_to_tray(r#"{"close_to_tray":"yes"}"#), None);
        assert_eq!(parse_close_to_tray("not-json"), None);
    }

    #[test]
    fn webview2_data_dir_prefers_the_application_directory() {
        let dir = TestDir::new("webview2");
        let chosen = webview2_data_dir(Some(dir.path())).expect("writable exe dir");
        assert_eq!(chosen, dir.path().join("webview2-data"));
    }

    #[test]
    fn writable_probe_matches_the_backend_contract() {
        let dir = TestDir::new("writable");
        assert!(dir_is_writable(dir.path()));
        // 用一个文件冒充目录：mkdir 必然失败。
        let blocked = dir.path().join("blocked");
        std::fs::write(&blocked, b"not a dir").unwrap();
        assert!(!dir_is_writable(&blocked));
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
    fn fast_restarts_grow_the_failure_count_and_a_stable_generation_resets_it() {
        assert_eq!(next_generation_failures(0, Duration::from_secs(1)), 1);
        assert_eq!(next_generation_failures(3, Duration::from_secs(59)), 4);
        assert_eq!(next_generation_failures(4, Duration::from_secs(60)), 0);
        assert_eq!(
            next_generation_failures(u32::MAX, Duration::from_secs(1)),
            u32::MAX
        );
    }

    #[test]
    fn session_is_not_published_after_shutdown() {
        let supervisor = BackendSupervisor::new(17520);
        supervisor.request_shutdown();
        assert!(!supervisor.set_session("token".into()));
        assert!(supervisor.current_session().is_none());
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
        let dir = TestDir::new("log");
        let path = dir.path().join("logs").join("launcher.log");

        append_launcher_log_at(&path, 1000, "backend restarted").unwrap();
        append_launcher_log_at(&path, 1001, "backend exited").unwrap();

        let contents = std::fs::read_to_string(&path).unwrap();
        assert!(contents.contains("[1000] backend restarted"));
        assert!(contents.contains("[1001] backend exited"));
    }

    #[test]
    fn launcher_log_rotates_when_it_exceeds_the_limit() {
        let dir = TestDir::new("log-rotate");
        let path = dir.path().join("logs").join("launcher.log");

        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, vec![b'x'; LAUNCHER_LOG_MAX_BYTES as usize]).unwrap();

        append_launcher_log_at(&path, 2000, "after rotation").unwrap();

        let backup = path.with_extension("log.1");
        assert!(backup.exists(), "超限后未轮转出 .1 备份");
        let contents = std::fs::read_to_string(&path).unwrap();
        assert!(contents.contains("[2000] after rotation"));
        assert!(!contents.contains('x'), "新文件不该带上旧内容");
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
        let tool = system_tool("taskkill.exe").expect("taskkill.exe must resolve on Windows");
        assert!(tool.is_absolute(), "{tool:?}");
        // GetSystemDirectoryW 返回的路径大小写随系统（实测 "system32"），
        // 这里按 Windows 的路径语义做大小写无关比较。
        let rendered = tool.to_string_lossy().to_ascii_lowercase();
        assert!(rendered.ends_with("system32\\taskkill.exe"), "{tool:?}");
    }

    #[test]
    #[cfg(windows)]
    fn missing_system_tools_are_disabled_instead_of_falling_back_to_a_bare_name() {
        // 裸名会优先命中应用目录/CWD（用户可写）。找不到绝对路径时必须返回
        // None，让调用方禁用该功能，而不是执行一个同名的植入件。
        assert!(system_tool("lunarshroud-no-such-tool.exe").is_none());
    }

    #[test]
    #[cfg(not(windows))]
    fn system_tools_are_disabled_without_systemroot() {
        assert!(system_tool("taskkill.exe").is_none());
    }

    #[test]
    #[cfg(windows)]
    fn wait_for_process_exit_observes_a_real_child() {
        let mut child = std::process::Command::new("cmd")
            .args(["/C", "exit 0"])
            .spawn()
            .expect("spawn cmd");
        let pid = child.id();
        assert!(wait_for_process_exit(pid, Duration::from_secs(5)));
        let _ = child.wait();
    }

    #[test]
    #[cfg(windows)]
    fn wait_for_process_exit_returns_false_for_a_missing_pid() {
        assert!(!wait_for_process_exit(u32::MAX, Duration::from_millis(10)));
    }
}
