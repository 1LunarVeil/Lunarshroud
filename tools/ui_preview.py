"""前端视觉检查：无头浏览器截图 + console 错误采集。

为什么需要它
------------
改前端最大的问题是「看不见自己做的界面」，只能靠想象，于是审美和一致性
都会漂移。这个脚本让改动可以被"回看"：打开真实页面、执行几步交互、截图、
并把 console 报错一起带回来。

为什么可以不用 Tauri
-------------------
`frontend/js/api.js` 在非 Tauri 环境下会降级到 `location.origin`，因此
`python backend/main.py` 起一个开发后端（前端由同一个进程静态托管）后，
用普通 Chromium 打开 http://127.0.0.1:17520/ 就能完整调试整个界面。

依赖：Chrome 或 Edge（自动探测常见安装路径），Python `requests` / `websockets`
（二者已是 backend 依赖）。

用法
----
    # 只截首页
    python tools/ui_preview.py --out tmp/shots/home.png

    # 打开参数编辑器，展开「推测解码」分组，再全页截图
    python tools/ui_preview.py --out tmp/shots/spec.png \
        --page param_editor --expand speculative --full-page

    # 顺便探测 DOM（把表达式结果打印出来）
    python tools/ui_preview.py --out tmp/shots/spec.png \
        --page param_editor --expand speculative \
        --probe "document.querySelectorAll('.option-card').length"
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

try:  # websockets >= 11
    import websockets
except ImportError:  # pragma: no cover
    print("缺少依赖：pip install websockets", file=sys.stderr)
    raise

BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def find_browser(explicit: str | None) -> str:
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise SystemExit(f"指定的浏览器不存在: {explicit}")
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("chrome") or shutil.which("chromium") or shutil.which("msedge")
    if found:
        return found
    raise SystemExit("找不到 Chrome/Edge，请用 --browser 指定可执行文件路径")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_page_target(port: int, timeout: float = 25.0) -> str:
    """轮询 /json/list，返回第一个 page 目标的 WebSocket 调试地址。"""
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2)
            for target in response.json():
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
        time.sleep(0.3)
    raise SystemExit(f"等待浏览器就绪超时（{timeout}s）：{last_error}")


class CdpSession:
    """极简 CDP 客户端：只实现本脚本需要的调用与事件收集。"""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.console_errors: list[str] = []
        self._pump: asyncio.Task | None = None

    async def start(self) -> None:
        self._pump = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:  # noqa: PLR1702 - 单层循环，扁平结构
                message = json.loads(raw)
                if "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future and not future.done():
                        future.set_result(message)
                    continue
                self._on_event(message)
        except Exception:  # 连接关闭 / 解析失败都不影响主流程
            pass

    def _on_event(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails") or {}
            text = detail.get("text") or ""
            desc = ((detail.get("exception") or {}).get("description")) or ""
            self.console_errors.append(f"[pageerror] {text} {desc}".strip())
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            parts = []
            for arg in params.get("args") or []:
                parts.append(str(arg.get("value", arg.get("description", ""))))
            self.console_errors.append("[console.error] " + " ".join(parts))
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") == "error":
                self.console_errors.append(f"[{entry.get('source')}] {entry.get('text')}")

    async def send(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        return await asyncio.wait_for(future, timeout=timeout)

    async def evaluate(self, expression: str):
        """执行表达式；出错时返回可读的错误字符串而不是抛异常。

        探测只是辅助手段，不应该让整次截图失败。
        """
        result = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        payload = result.get("result") or {}
        details = payload.get("exceptionDetails")
        if details:
            text = details.get("text") or ""
            desc = ((details.get("exception") or {}).get("description")) or ""
            first_line = desc.splitlines()[0] if desc else ""
            return f"<error> {text} {first_line}".strip()
        # CDP 也会用 protocol error 回包（例如表达式语法错误）
        if payload.get("error"):
            return f"<error> {payload['error']}"
        inner = payload.get("result") or {}
        return inner.get("value")

    async def stop(self) -> None:
        if self._pump:
            self._pump.cancel()


def report_diff(baseline_path: str, current_path: Path, skip_top: int,
                image_out: str | None = None) -> None:
    """与基准截图逐像素对比。

    样式重构（例如把写死的颜色迁移进 tokens）声称"零视觉回归"时，必须用
    数字证明，不能靠肉眼 —— 缩放后的缩略图会掩盖裁剪和对比度问题。
    """
    try:
        from PIL import Image, ImageChops
    except ImportError:
        print("[对比] 需要 Pillow：pip install Pillow", file=sys.stderr)
        return

    baseline = Path(baseline_path)
    if not baseline.is_file():
        print(f"[对比] 基准截图不存在: {baseline}", file=sys.stderr)
        return

    try:
        image_a = Image.open(baseline).convert("RGB")
        image_b = Image.open(current_path).convert("RGB")
    except OSError as exc:
        print(f"[对比] 无法读取图像: {exc}", file=sys.stderr)
        return

    if image_a.size != image_b.size:
        width = min(image_a.size[0], image_b.size[0])
        height = min(image_a.size[1], image_b.size[1])
        image_a = image_a.crop((0, 0, width, height))
        image_b = image_b.crop((0, 0, width, height))
        print(f"[对比] 尺寸不同，已裁到公共区域 {image_a.size}")

    width, height = image_a.size
    box = (0, skip_top, width, height)
    diff = ImageChops.difference(image_a.crop(box), image_b.crop(box))
    # 逐通道取最大值后统计非零像素（避免 getdata() 的弃用告警）
    red, green, blue = diff.split()
    combined = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    differing = sum(combined.histogram()[1:])
    total = (height - skip_top) * width
    ratio = differing / total * 100 if total else 0

    if image_out and differing:
        # 把差异像素标红叠加到当前截图上，便于直接看清"哪里变了"
        mask = combined.point(lambda value: 255 if value else 0).convert("L")
        overlay = image_b.crop(box).copy()
        overlay.paste(Image.new("RGB", overlay.size, (255, 0, 0)), (0, 0), mask)
        overlay_path = Path(image_out)
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay.save(overlay_path)
        print(f"[对比] 差异可视化已写入 {overlay_path}")

    if differing == 0:
        print(f"[对比] 与基准完全一致（已排除顶部 {skip_top}px）-> 零视觉回归")
        return
    bbox = diff.getbbox()
    where = ""
    if bbox:
        where = (f" 差异范围 x={bbox[0]}..{bbox[2]} y={bbox[1] + skip_top}..{bbox[3] + skip_top}")
    print(f"[对比] {differing} / {total} 像素不同（{ratio:.4f}%）{where}")
    print("[对比] 差异不为零，请人工核对；若差异集中在状态栏/时间等动态区域，用 --compare-skip-top 排除")


def build_actions(args) -> list[tuple[str, str]]:
    """构造要在页面里顺序执行的表达式列表。"""
    actions: list[tuple[str, str]] = []
    if args.page:
        safe = json.dumps(args.page)
        actions.append((
            f"切到页面 {args.page}",
            f"""(() => {{
                const item = document.querySelector('.sidebar-item[data-page={safe}]');
                if (!item) return 'no-sidebar-item';
                item.click();
                return 'ok';
            }})()""",
        ))
    if args.expand:
        safe = json.dumps(args.expand)
        actions.append((
            f"展开分组 {args.expand}",
            f"""(() => {{
                // param_editor 模态化后是切换式分组导航：先点导航项让该分组渲染
                const navBtn = document.querySelector(`.pe-nav [data-goto-group={safe}]`);
                if (navBtn) navBtn.click();
                const group = document.querySelector('.collapse[data-group=' + {safe} + ']');
                if (!group) return navBtn ? 'no-group-after-nav' : 'no-group';
                if (!group.classList.contains('open')) {{
                    group.querySelector('.collapse-header').click();
                }}
                group.scrollIntoView({{ block: 'center' }});
                return 'ok';
            }})()""",
        ))
    return actions


async def capture(args) -> int:
    browser = find_browser(args.browser)
    port = free_port()
    profile = tempfile.mkdtemp(prefix="lunarshroud-ui-preview-")
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        f"--window-size={args.width},{args.height}",
        args.url,
    ]
    if args.dark:
        command.insert(-1, "--force-dark-mode")

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out_path = Path(args.out)
    try:
        ws_url = wait_for_page_target(port)
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            session = CdpSession(ws)
            await session.start()
            await session.send("Page.enable")
            await session.send("Runtime.enable")
            await session.send("Log.enable")

            # --theme：应用主题存 localStorage，必须在 boot 前写入才生效，
            # 所以用 addScriptToEvaluateOnNewDocument + reload（app 默认是 dark）
            if args.theme:
                await session.send(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {
                        "source": (
                            "try{localStorage.setItem('lunarshroud_theme',"
                            f"{json.dumps(args.theme)})}}catch(e){{}}"
                        )
                    },
                )
                await session.send("Page.reload")

            await asyncio.sleep(args.settle)
            for label, expression in build_actions(args):
                outcome = await session.evaluate(expression)
                print(f"[动作] {label} -> {outcome}")
                await asyncio.sleep(args.step_delay)

            # 先跑附加脚本（准备状态），再探测（读取结果），最后截图
            if args.extra_js:
                for snippet in args.extra_js:
                    print(f"[附加] {await session.evaluate(snippet)}")
                await asyncio.sleep(args.step_delay)

            if args.probe:
                expression = args.probe
                if expression.startswith("@"):
                    expression = Path(expression[1:]).read_text(encoding="utf-8")
                value = await session.evaluate(expression)
                if args.probe_out:
                    out_file = Path(args.probe_out)
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    out_file.write_text(str(value), encoding="utf-8")
                    print(f"[探测] 结果已写入 {out_file}（{len(str(value))} 字符）")
                else:
                    print(f"[探测] -> {value}")

            params: dict = {"format": "png"}
            if args.full_page:
                metrics = await session.send("Page.getLayoutMetrics")
                content = (metrics.get("result") or {}).get("cssContentSize") or {}
                width = int(content.get("width") or args.width)
                height = int(content.get("height") or args.height)
                params["clip"] = {"x": 0, "y": 0, "width": width, "height": height, "scale": 1}
                params["captureBeyondViewport"] = True

            shot = await session.send("Page.captureScreenshot", params)
            data = ((shot.get("result") or {}).get("data")) or ""
            if not data:
                print(f"截图失败: {shot}", file=sys.stderr)
                return 2
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(base64.b64decode(data))
            print(f"[截图] {out_path}  ({out_path.stat().st_size / 1024:.0f} KB)")

            if session.console_errors:
                print(f"[console] 发现 {len(session.console_errors)} 条错误：")
                for line in session.console_errors[:20]:
                    print("   " + line)
            else:
                print("[console] 无错误")
            await session.stop()

        if args.compare:
            report_diff(args.compare, out_path, args.compare_skip_top, args.compare_image)
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="前端视觉检查（无头浏览器截图 + console 采集）")
    parser.add_argument("--url", default="http://127.0.0.1:17520/", help="目标地址")
    parser.add_argument("--out", default="tmp/shots/preview.png", help="截图输出路径")
    parser.add_argument("--page", help="打开后切到的页面模块 id（如 param_editor）")
    parser.add_argument("--expand", help="要展开的折叠分组 key（如 speculative）")
    parser.add_argument("--probe",
                        help="DOM 探测表达式，结果会打印；传 @路径 可从文件读取（长表达式推荐，"
                             "避免 Windows PowerShell 吃掉内嵌双引号）")
    parser.add_argument("--probe-out",
                        help="把探测结果写入文件，用于结构对比（例如合并 CSS 前后导出级联声明表）")
    parser.add_argument("--extra-js", action="append", help="额外执行的 JS（可重复）")
    parser.add_argument("--full-page", action="store_true", help="整页截图而非仅视口")
    parser.add_argument("--compare", help="与基准截图逐像素对比（需要 Pillow），用于证明零视觉回归")
    parser.add_argument("--compare-skip-top", type=int, default=0,
                        help="对比时忽略顶部 N 像素行（排除状态栏等动态区域）")
    parser.add_argument("--compare-image",
                        help="把差异像素标红后另存为图片，便于直接看清差异位置")
    parser.add_argument("--dark", action="store_true", help="强制深色模式")
    parser.add_argument("--theme", choices=["light", "dark"],
                        help="应用主题（写入 localStorage 后整页 reload；默认不干预，即 app 默认 dark）")
    parser.add_argument("--browser", help="浏览器可执行文件路径")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--settle", type=float, default=1.5, help="页面加载后等待秒数")
    parser.add_argument("--step-delay", type=float, default=0.5, help="每步交互后的等待秒数")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(capture(args))


if __name__ == "__main__":
    raise SystemExit(main())
