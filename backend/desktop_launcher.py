from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser


def build_server_config() -> dict:
    return {
        "app": "single_port_app:app",
        "host": os.environ.get("ULTRARAG_HOST", "127.0.0.1"),
        "port": int(os.environ.get("ULTRARAG_PORT", "5173")),
    }


def _redirect_logs_when_frozen() -> None:
    # console=False 的 GUI app 没有终端，stdout/stderr 会丢失。
    # 打包态把日志重定向到用户数据目录，启动/运行问题可查文件。
    if not getattr(sys, "frozen", False):
        return
    try:
        from app import paths
        log_dir = paths.user_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "staffdeck.log"
        previous_log_path = log_dir / "staffdeck.previous.log"
        if log_path.exists():
            log_path.replace(previous_log_path)
        log_file = open(log_path, "w", buffering=1, encoding="utf-8")
        sys.stdout = log_file
        sys.stderr = log_file
        print(f"URStaff session started: pid={os.getpid()}")
    except Exception:
        pass


def apply_runtime_env() -> None:
    # 时序契约：必须在任何 app.config 被 import 之前调用；仅 frozen 态断言，
    # 开发/测试进程通常已 import 过 app.config，无条件断言会误炸。
    if getattr(sys, "frozen", False):
        assert "app.config" not in sys.modules, "apply_runtime_env 必须在 import app.* 之前调用"

    cfg = build_server_config()
    origin = f"http://{cfg['host']}:{cfg['port']}"
    os.environ.setdefault("TOOL_BASE_URL", origin)
    existing_cors = os.environ.get("CORS_ORIGINS", "")
    if origin not in existing_cors:
        os.environ["CORS_ORIGINS"] = ",".join(filter(None, [existing_cors, origin]))

    # frozen 态把 .env 指向用户数据目录（不存在则 pydantic 不加载），避免误加载启动 cwd 的陌生 .env
    if getattr(sys, "frozen", False):
        from app import paths
        os.environ.setdefault("ULTRARAG_DOTENV", str(paths.user_data_dir() / ".env"))


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _open_browser_when_ready(url: str) -> None:
    import urllib.request

    for _ in range(120):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1):
                _open_browser(url + "/chat/")
                return
        except Exception:
            time.sleep(0.5)


def _open_browser(target: str) -> None:
    """打开浏览器页面。点 Dock 图标每次都开一个新标签——最稳定、跨浏览器一致、
    不依赖 macOS 自动化授权（adhoc 签名下自动化授权弹窗不可靠）。"""
    webbrowser.open(target)



def _use_macos_dock_app() -> bool:
    # 仅 macOS 打包态用 Cocoa 壳（进 Dock + 点图标开页面）。
    # 开发态 / 其它平台保持简单主线程 uvicorn。
    return sys.platform == "darwin" and getattr(sys, "frozen", False)


def _use_windows_taskbar_app() -> bool:
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def _is_windows_restore_command(message: int, wparam: int) -> bool:
    wm_syscommand = 0x0112
    sc_restore = 0xF120
    return message == wm_syscommand and (wparam & 0xFFF0) == sc_restore


def _serve(cfg: dict) -> None:
    import uvicorn

    uvicorn.run(cfg["app"], host=cfg["host"], port=cfg["port"], log_level="info")


def _run_macos_dock_app(cfg: dict, url: str) -> int:
    """macOS：NSApplication 主循环。进 Dock、点 Dock 图标重新打开浏览器、退出时停服务。"""
    import AppKit
    from PyObjCTools import AppHelper

    # uvicorn 在后台线程跑（主线程要留给 Cocoa 事件循环）
    server_thread = threading.Thread(target=_serve, args=(cfg,), daemon=True)
    server_thread.start()
    threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

    class AppDelegate(AppKit.NSObject):
        def applicationDidFinishLaunching_(self, _notification):  # noqa: N802
            print(f"URStaff 启动中，就绪后将打开：{url}/chat/")

        def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _flag):  # noqa: N802
            # 点 Dock 图标（app 已在运行）→ 打开浏览器页面（新标签）
            _open_browser(url + "/chat/")
            return True

        def applicationShouldTerminate_(self, _app):  # noqa: N802
            return AppKit.NSTerminateNow

    app = AppKit.NSApplication.sharedApplication()
    # Regular：常规 GUI app，进 Dock、可激活
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()
    return 0


def _run_windows_taskbar_app(cfg: dict, url: str) -> int:
    """Run the server behind a native window so URStaff owns a taskbar icon."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    WM_DESTROY = 0x0002
    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG = 1
    WS_OVERLAPPEDWINDOW = 0x00CF0000
    WS_EX_APPWINDOW = 0x00040000
    SW_SHOWMINIMIZED = 2
    SW_SHOWMINNOACTIVE = 7
    CW_USEDEFAULT = -2147483648
    COLOR_WINDOW = 5

    WNDPROC = ctypes.WINFUNCTYPE(
        wintypes.LPARAM,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
    shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
    shell32.ExtractIconExW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HICON),
        ctypes.POINTER(wintypes.HICON),
        wintypes.UINT,
    ]
    shell32.ExtractIconExW.restype = wintypes.UINT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = wintypes.LPARAM
    user32.SendMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL

    shell32.SetCurrentProcessExplicitAppUserModelID("ai.urstaff.desktop")
    large_icon = wintypes.HICON()
    small_icon = wintypes.HICON()
    shell32.ExtractIconExW(sys.executable, 0, ctypes.byref(large_icon), ctypes.byref(small_icon), 1)

    @WNDPROC
    def window_proc(hwnd, message, wparam, lparam):
        if _is_windows_restore_command(message, wparam):
            print("Taskbar activated; opening URStaff in the system default browser.")
            _open_browser(url + "/chat/")
            user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    class_name = "URStaffDesktopWindow"
    window_class = WNDCLASSW()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = instance
    window_class.hIcon = large_icon
    window_class.hCursor = user32.LoadCursorW(None, 32512)
    window_class.hbrBackground = COLOR_WINDOW + 1
    window_class.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        error = ctypes.get_last_error()
        if error != 1410:  # ERROR_CLASS_ALREADY_EXISTS
            raise ctypes.WinError(error)

    hwnd = user32.CreateWindowExW(
        WS_EX_APPWINDOW,
        class_name,
        "URStaff",
        WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT,
        CW_USEDEFAULT,
        430,
        190,
        None,
        None,
        instance,
        None,
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    if large_icon:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, ctypes.cast(large_icon, ctypes.c_void_p).value)
    if small_icon:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, ctypes.cast(small_icon, ctypes.c_void_p).value)

    print(
        f"Windows shell ready: hwnd={hwnd}, "
        f"large_icon={ctypes.cast(large_icon, ctypes.c_void_p).value or 0}, "
        f"small_icon={ctypes.cast(small_icon, ctypes.c_void_p).value or 0}"
    )

    threading.Thread(target=_serve, args=(cfg,), daemon=True).start()
    threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()
    user32.ShowWindow(hwnd, SW_SHOWMINIMIZED)
    user32.UpdateWindow(hwnd)

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))

    if large_icon:
        user32.DestroyIcon(large_icon)
    if small_icon:
        user32.DestroyIcon(small_icon)
    return 0


def main(argv: list[str] | None = None) -> int:
    # 时序：先设 env（apply_runtime_env），再 import uvicorn / 触发 app.* import。
    apply_runtime_env()

    cfg = build_server_config()
    url = f"http://{cfg['host']}:{cfg['port']}"

    # 已在运行：直接开浏览器并退出（双击重复启动的兜底）
    if port_in_use(cfg["host"], cfg["port"]):
        try:
            import urllib.request
            with urllib.request.urlopen(url + "/api/health", timeout=1):
                print(f"URStaff 已在运行：{url}/chat/")
                _open_browser(url + "/chat/")
                return 0
        except Exception:
            print(f"端口 {cfg['port']} 已被其它程序占用。请设置 ULTRARAG_PORT 换端口后重试。", file=sys.stderr)
            return 2

    _redirect_logs_when_frozen()

    if _use_macos_dock_app():
        return _run_macos_dock_app(cfg, url)

    if _use_windows_taskbar_app():
        return _run_windows_taskbar_app(cfg, url)

    # 其它平台 / 开发态：主线程跑 uvicorn，后台线程开浏览器
    print(f"URStaff 启动中，就绪后将打开：{url}/chat/")
    threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()
    _serve(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
