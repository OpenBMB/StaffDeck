from __future__ import annotations

import argparse
import importlib
import ipaddress
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import urlsplit

from app.version import app_version

APP_NAME = "StaffDeck"
APP_ID = "ai.staffdeck.desktop"
APP_VERSION = app_version()
NETWORK_MODES = {"local", "lan", "public"}
DEFAULT_PORT_RANGE_START = 5173
DEFAULT_PORT_RANGE_END = 5199
_MACOS_DELEGATE_REF = None
_MACOS_INSTANCE_LOCK_HANDLE = None
STAFFDECK_ICON_PNG = ("packaging", "assets", "staffdeck.png")
LARK_PACKAGING_SMOKE_IMPORTS = (
    ("lark_channel", "EventDispatcherHandler"),
    ("lark_channel.ws.client", "Client"),
    ("lark_channel.ws.pb.pbbp2_pb2", "Frame"),
)


def build_server_config() -> dict:
    host = os.environ.get("ULTRARAG_HOST", "127.0.0.1")
    return {
        "app": "single_port_app:app",
        "host": host,
        "port": find_available_port(host),
        "public_url": os.environ.get("STAFFDECK_PUBLIC_URL", "").strip(),
    }


def _network_config_path() -> Path:
    return Path(user_data_dir()) / "network.json"


def user_data_dir() -> Path:
    from app import paths

    return paths.user_data_dir()


def _load_network_config() -> dict[str, str]:
    try:
        payload = json.loads(_network_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_network_config(mode: str, host: str, port: int, public_url: str = "") -> Path:
    if mode not in NETWORK_MODES:
        raise ValueError(f"网络模式必须是 local、lan 或 public，当前为 {mode!r}")
    if mode == "local":
        host = "127.0.0.1"
    elif mode in {"lan", "public"}:
        host = "0.0.0.0"
    if mode == "public" and not public_url:
        raise ValueError("公网模式需要提供 --public-url，例如 https://staff.example.com")
    path = _network_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"mode": mode, "host": host, "port": port, "public_url": public_url}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return path


def _infer_public_url(port: int) -> str:
    candidates: list[str] = []
    try:
        candidates.append(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    for host in candidates:
        if not host:
            continue
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified:
            continue
        return f"http://{host}:{port}"
    return ""


def _apply_network_config(argv: list[str]) -> list[str]:
    """Apply persisted/CLI network settings before importing the ASGI app."""
    saved = _load_network_config()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", choices=sorted(NETWORK_MODES))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--public-url")
    args, remaining = parser.parse_known_args(argv)
    mode = args.mode or str(saved.get("mode") or "local")
    host = args.host or os.environ.get("ULTRARAG_HOST") or str(
        saved.get("host") or ("127.0.0.1" if mode == "local" else "0.0.0.0")
    )
    port = args.port or int(os.environ.get("ULTRARAG_PORT") or saved.get("port") or 5173)
    public_url = args.public_url or os.environ.get("STAFFDECK_PUBLIC_URL") or str(
        saved.get("public_url") or ""
    )
    if mode == "local":
        host = "127.0.0.1"
    elif args.mode and mode in {"lan", "public"} and not args.host:
        host = "0.0.0.0"
    os.environ["ULTRARAG_HOST"] = host
    os.environ["ULTRARAG_PORT"] = str(port)
    if public_url:
        os.environ["STAFFDECK_PUBLIC_URL"] = public_url
    return remaining


def _setup_network(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="staffdeck setup", description="配置 StaffDeck 网络访问方式")
    parser.add_argument("--mode", choices=sorted(NETWORK_MODES), help="local、lan 或 public")
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--public-url", default="")
    args = parser.parse_args(argv)
    mode = args.mode
    if not mode:
        if not sys.stdin.isatty():
            raise SystemExit("无头环境请使用 staffdeck setup --mode local|lan|public")
        print("选择网络模式：1) 本机  2) 局域网  3) 公网")
        mode = {"1": "local", "2": "lan", "3": "public"}.get(input("请选择 [1]: ").strip() or "1")
        if not mode:
            raise SystemExit("无效的网络模式")
    public_url = args.public_url.strip()
    if mode == "public" and not public_url:
        public_url = _infer_public_url(args.port)
        if not public_url and not sys.stdin.isatty():
            raise SystemExit("公网模式必须提供 --public-url")
        if not public_url:
            public_url = input("请输入公网 URL（例如 https://staff.example.com）：").strip()
    path = _save_network_config(mode, "", args.port, public_url)
    print(f"已保存网络模式：{mode}，配置文件：{path}")
    return 0


def _redirect_logs_when_frozen() -> None:
    if not getattr(sys, "frozen", False):
        return
    try:
        from app.runtime_logging import configure_runtime_logging

        configure_runtime_logging()
    except Exception:
        pass


def _run_packaging_smoke() -> int:
    from feishu_connector_worker import SDK_CONTRACT_VERSION

    actual_version = importlib_metadata.version("lark-channel-sdk")
    if actual_version != SDK_CONTRACT_VERSION:
        raise RuntimeError(
            f"lark-channel-sdk must be exactly {SDK_CONTRACT_VERSION}, got {actual_version}"
        )
    for module_name, symbol_name in LARK_PACKAGING_SMOKE_IMPORTS:
        module = importlib.import_module(module_name)
        if not hasattr(module, symbol_name):
            raise RuntimeError(f"{module_name} is missing required symbol {symbol_name}")
    print(f"packaging smoke ok: lark-channel-sdk=={actual_version}")
    return 0


def apply_runtime_env(cfg: dict | None = None) -> None:
    # 时序契约：必须在任何 app.config 被 import 之前调用；仅 frozen 态断言，
    # 开发/测试进程通常已 import 过 app.config，无条件断言会误炸。
    if getattr(sys, "frozen", False):
        assert "app.config" not in sys.modules, "apply_runtime_env 必须在 import app.* 之前调用"

    cfg = cfg or build_server_config()
    local_origin = f"http://{('127.0.0.1' if cfg['host'] == '0.0.0.0' else cfg['host'])}:{cfg['port']}"
    origin = cfg.get("public_url") or local_origin
    os.environ.setdefault("TOOL_BASE_URL", local_origin)
    existing_cors = os.environ.get("CORS_ORIGINS", "")
    origins = [item for item in (existing_cors, local_origin, origin) if item]
    os.environ["CORS_ORIGINS"] = ",".join(dict.fromkeys(",".join(origins).split(",")))

    # frozen 态把 .env 指向用户数据目录（不存在则 pydantic 不加载），避免误加载启动 cwd 的陌生 .env
    if getattr(sys, "frozen", False):
        from app import paths
        os.environ.setdefault("ULTRARAG_DOTENV", str(paths.user_data_dir() / ".env"))


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值：{raw!r}") from exc


def _port_candidates() -> list[int]:
    start = _env_int("ULTRARAG_PORT_RANGE_START", DEFAULT_PORT_RANGE_START)
    end = _env_int("ULTRARAG_PORT_RANGE_END", DEFAULT_PORT_RANGE_END)
    if start > end:
        start, end = end, start

    candidates = list(range(start, end + 1))
    explicit = os.environ.get("ULTRARAG_PORT")
    if explicit:
        port = _env_int("ULTRARAG_PORT", DEFAULT_PORT_RANGE_START)
        candidates = [port] + [candidate for candidate in candidates if candidate != port]
    return candidates


def find_available_port(host: str) -> int:
    for port in _port_candidates():
        if not port_in_use(host, port):
            return port
    first, last = _port_candidates()[0], _port_candidates()[-1]
    raise RuntimeError(f"{APP_NAME} 可用端口耗尽：{first}-{last} 都已被占用")


def _resource_path(*parts: str) -> str | None:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass, *parts))

    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        candidates.append(executable.parent.joinpath(*parts))
        if sys.platform == "darwin" and len(executable.parents) >= 2:
            candidates.append(executable.parents[1] / "Resources" / Path(*parts))

    candidates.append(Path(__file__).resolve().parent.parent.joinpath(*parts))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _staffdeck_icon_png_path() -> str | None:
    return _resource_path(*STAFFDECK_ICON_PNG)


def _health_ok(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("status") == "ok" and payload.get("app") == APP_NAME
    except Exception:
        return False


def _find_existing_app_url(host: str) -> str | None:
    for port in _port_candidates():
        if not port_in_use(host, port):
            continue
        url = f"http://{host}:{port}"
        if _health_ok(url):
            return url
    return None


def _wait_for_existing_app_url(host: str, attempts: int = 20, delay: float = 0.3) -> str | None:
    for _ in range(attempts):
        url = _find_existing_app_url(host)
        if url:
            return url
        time.sleep(delay)
    return None


def _acquire_macos_instance_lock() -> bool:
    if not _use_macos_dock_app():
        return True

    try:
        import fcntl
    except Exception:
        return True

    global _MACOS_INSTANCE_LOCK_HANDLE
    lock_path = Path(tempfile.gettempdir()) / f"{APP_ID}.lock"
    lock_file = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return False
    lock_file.seek(0)
    lock_file.write(str(os.getpid()))
    lock_file.truncate()
    lock_file.flush()
    _MACOS_INSTANCE_LOCK_HANDLE = lock_file
    return True


def _open_browser_when_ready(url: str) -> None:
    for _ in range(120):
        if _health_ok(url):
            _open_browser(url + "/chat/")
            return
        time.sleep(0.5)


def _open_browser(target: str) -> None:
    """Open StaffDeck in the system browser on platforms without an embedded window."""
    webbrowser.open(target)


def _is_external_web_url(target: str, local_url: str) -> bool:
    """Return whether a web URL should leave the embedded StaffDeck window."""
    target_parts = urlsplit(target)
    local_parts = urlsplit(local_url)
    if target_parts.scheme not in {"http", "https"} or not target_parts.hostname:
        return False
    try:
        target_port = target_parts.port or (443 if target_parts.scheme == "https" else 80)
        local_port = local_parts.port or (443 if local_parts.scheme == "https" else 80)
    except ValueError:
        return False
    return (target_parts.scheme, target_parts.hostname, target_port) != (
        local_parts.scheme,
        local_parts.hostname,
        local_port,
    )


def _four_char_code(value: str) -> int:
    result = 0
    for byte in value.encode("macroman"):
        result = (result << 8) | byte
    return result


def _use_macos_dock_app() -> bool:
    if _env_flag("STAFFDECK_HEADLESS"):
        return False
    # 仅 macOS 打包态用 Cocoa 壳和内嵌 WebView。
    return sys.platform == "darwin" and getattr(sys, "frozen", False)


def _use_windows_taskbar_app() -> bool:
    if _env_flag("STAFFDECK_HEADLESS"):
        return False
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_windows_restore_command(message: int, wparam: int) -> bool:
    wm_syscommand = 0x0112
    sc_restore = 0xF120
    return message == wm_syscommand and (wparam & 0xFFF0) == sc_restore


def _serve(cfg: dict) -> None:
    import uvicorn

    if getattr(sys, "frozen", False):
        logging.getLogger("staffdeck.runtime").info(
            "Server starting host=%s port=%s",
            cfg["host"],
            cfg["port"],
        )
        uvicorn.run(
            cfg["app"],
            host=cfg["host"],
            port=cfg["port"],
            log_level="info",
            log_config=None,
            access_log=False,
        )
        return
    uvicorn.run(cfg["app"], host=cfg["host"], port=cfg["port"], log_level="info")


def preload_server_app(cfg: dict) -> None:
    app_ref = cfg.get("app")
    if not isinstance(app_ref, str):
        return
    module_name, separator, attribute_name = app_ref.partition(":")
    if not separator or not module_name or not attribute_name:
        raise RuntimeError(f"Invalid ASGI application reference: {app_ref!r}")
    module = importlib.import_module(module_name)
    cfg["app"] = getattr(module, attribute_name)


def _create_macos_webview_window(AppKit, Foundation, WebKit, target: str):
    """Create the native macOS window used by both arm64 and x86_64 bundles."""
    style = (
        AppKit.NSWindowStyleMaskTitled
        | AppKit.NSWindowStyleMaskClosable
        | AppKit.NSWindowStyleMaskMiniaturizable
        | AppKit.NSWindowStyleMaskResizable
    )
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(0, 0, 1280, 800),
        style,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    window.setTitle_(APP_NAME)
    window.setMinSize_(AppKit.NSMakeSize(900, 600))
    window.setReleasedWhenClosed_(False)
    window.center()

    webview = WebKit.WKWebView.alloc().initWithFrame_(window.contentView().bounds())
    webview.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    page_url = Foundation.NSURL.URLWithString_(target)
    if page_url is None:
        raise RuntimeError(f"Invalid StaffDeck window URL: {target!r}")
    webview.loadRequest_(Foundation.NSURLRequest.requestWithURL_(page_url))
    window.setContentView_(webview)
    window.makeKeyAndOrderFront_(None)
    return window, webview


def _create_macos_main_menu(AppKit, app_delegate):
    """Create the standard app and edit menus used by the native macOS shell."""
    main_menu = AppKit.NSMenu.alloc().initWithTitle_(APP_NAME)

    app_menu_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        APP_NAME, None, ""
    )
    main_menu.addItem_(app_menu_item)
    app_menu = AppKit.NSMenu.alloc().initWithTitle_(APP_NAME)
    app_menu_item.setSubmenu_(app_menu)

    about_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"关于 {APP_NAME}", "showAbout:", ""
    )
    about_item.setTarget_(app_delegate)
    app_menu.addItem_(about_item)
    app_menu.addItem_(AppKit.NSMenuItem.separatorItem())

    hide_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"隐藏 {APP_NAME}", "hide:", "h"
    )
    app_menu.addItem_(hide_item)
    hide_others_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "隐藏其他", "hideOtherApplications:", "h"
    )
    hide_others_item.setKeyEquivalentModifierMask_(
        AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagOption
    )
    app_menu.addItem_(hide_others_item)
    app_menu.addItem_(
        AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "全部显示", "unhideAllApplications:", ""
        )
    )
    app_menu.addItem_(AppKit.NSMenuItem.separatorItem())

    quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"退出 {APP_NAME}", "quitStaffDeck:", "q"
    )
    quit_item.setTarget_(app_delegate)
    app_menu.addItem_(quit_item)

    edit_menu_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "编辑", None, ""
    )
    main_menu.addItem_(edit_menu_item)
    edit_menu = AppKit.NSMenu.alloc().initWithTitle_("编辑")
    edit_menu_item.setSubmenu_(edit_menu)

    edit_actions = (
        ("撤销", "undo:", "z"),
        ("重做", "redo:", "Z"),
        None,
        ("剪切", "cut:", "x"),
        ("拷贝", "copy:", "c"),
        ("粘贴", "paste:", "v"),
        ("全选", "selectAll:", "a"),
    )
    for action in edit_actions:
        if action is None:
            edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
            continue
        title, selector, key = action
        # A nil target sends the action through the responder chain to the focused WKWebView.
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, selector, key
        )
        edit_menu.addItem_(item)

    return main_menu


def _run_macos_dock_app(cfg: dict, url: str) -> int:
    """Run the local service behind a native WKWebView window on macOS."""
    import AppKit
    import Foundation
    import WebKit
    from PyObjCTools import AppHelper

    global _MACOS_DELEGATE_REF

    def load_app_icon(point_size: float | None = None):
        icon_path = _staffdeck_icon_png_path()
        if not icon_path:
            return None
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
        if image is not None and point_size is not None:
            image.setSize_((point_size, point_size))
        return image

    class WebViewNavigationDelegate(AppKit.NSObject):
        def webView_decidePolicyForNavigationAction_decisionHandler_(  # noqa: N802
            self,
            _webview,
            navigation_action,
            decision_handler,
        ):
            request_url = navigation_action.request().URL()
            target = str(request_url.absoluteString()) if request_url is not None else ""
            if _is_external_web_url(target, url):
                _open_browser(target)
                decision_handler(WebKit.WKNavigationActionPolicyCancel)
                return
            decision_handler(WebKit.WKNavigationActionPolicyAllow)

    class AppDelegate(AppKit.NSObject):
        def applicationDidFinishLaunching_(self, _notification):  # noqa: N802
            self.dock_visible = True
            self.server_started = False
            self.main_window = None
            self.main_webview = None
            self._install_url_scheme_handler()
            self._install_status_menu()
            self._start_server()
            print(f"{APP_NAME} 启动中，就绪后将显示应用窗口：{url}/chat/")

        def handleGetURLEvent_withReplyEvent_(self, event, _reply_event):  # noqa: N802
            direct_object = event.descriptorForKeyword_(_four_char_code("----"))
            deep_link = direct_object.stringValue() if direct_object is not None else ""
            print(f"收到 {APP_NAME} URL Scheme 唤起：{deep_link or '<empty>'}")
            self._show_window_when_ready()

        def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _flag):  # noqa: N802
            self.showMainWindow_(url + "/chat/")
            return True

        def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
            return False

        def applicationShouldTerminate_(self, _app):  # noqa: N802
            return AppKit.NSTerminateNow

        def applicationDockMenu_(self, _sender):  # noqa: N802
            # 右键 Dock 图标时展示同一套控制入口。
            self.dock_context_menu, self.dock_context_dock_item = self._build_control_menu()
            return self.dock_context_menu

        def openStaffDeck_(self, _sender):  # noqa: N802
            self.showMainWindow_(url + "/chat/")

        def showMainWindow_(self, target):
            if self.main_window is None:
                self.main_window, self.main_webview = _create_macos_webview_window(
                    AppKit,
                    Foundation,
                    WebKit,
                    str(target),
                )
                self.webview_navigation_delegate = WebViewNavigationDelegate.alloc().init()
                self.main_webview.setNavigationDelegate_(self.webview_navigation_delegate)
            else:
                self.main_window.makeKeyAndOrderFront_(None)
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        def restartStaffDeck_(self, _sender):  # noqa: N802
            os.execv(sys.executable, [sys.executable] + sys.argv[1:])

        def toggleDockIcon_(self, _sender):  # noqa: N802
            app = AppKit.NSApplication.sharedApplication()
            if self.dock_visible:
                app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
                self.dock_visible = False
                if hasattr(self, "status_dock_item"):
                    self.status_dock_item.setTitle_("显示 Dock 图标")
            else:
                app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
                app.activateIgnoringOtherApps_(True)
                self.dock_visible = True
                if hasattr(self, "status_dock_item"):
                    self.status_dock_item.setTitle_("隐藏 Dock 图标")

        def showAbout_(self, _sender):  # noqa: N802
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(APP_NAME)
            alert.setInformativeText_(f"版本：{APP_VERSION}\n本地服务：{url}")
            alert.addButtonWithTitle_("好")
            alert.runModal()

        def quitStaffDeck_(self, _sender):  # noqa: N802
            AppKit.NSApplication.sharedApplication().terminate_(self)

        def _start_server(self) -> None:
            if self.server_started:
                return
            self.server_started = True
            # uvicorn 在后台线程跑（主线程要留给 Cocoa 事件循环）。这里必须等
            # NSApplication 完成注册后再启动，避免 LaunchServices 初始化竞态导致 abort。
            threading.Thread(target=_serve, args=(cfg,), daemon=True).start()
            self._show_window_when_ready()

        def _show_window_when_ready(self) -> None:
            def wait_and_show() -> None:
                for _ in range(120):
                    if _health_ok(url):
                        AppHelper.callAfter(self.showMainWindow_, url + "/chat/")
                        return
                    time.sleep(0.5)

            threading.Thread(target=wait_and_show, daemon=True).start()

        def _install_url_scheme_handler(self) -> None:
            manager = AppKit.NSAppleEventManager.sharedAppleEventManager()
            manager.setEventHandler_andSelector_forEventClass_andEventID_(
                self,
                "handleGetURLEvent:withReplyEvent:",
                _four_char_code("GURL"),
                _four_char_code("GURL"),
            )

        def _menu_item(self, title: str, action: str | None = None, enabled: bool = True):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            item.setEnabled_(enabled)
            if action:
                item.setTarget_(self)
                item.setAction_(action)
            return item

        def _dock_toggle_title(self) -> str:
            return "隐藏 Dock 图标" if self.dock_visible else "显示 Dock 图标"

        def _build_control_menu(self):
            menu = AppKit.NSMenu.alloc().initWithTitle_(APP_NAME)
            menu.addItem_(self._menu_item("状态：运行中", enabled=False))
            menu.addItem_(self._menu_item(f"版本：{APP_VERSION}", enabled=False))
            menu.addItem_(self._menu_item(f"端口：{cfg['port']}", enabled=False))
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            menu.addItem_(self._menu_item(f"打开 {APP_NAME}", "openStaffDeck:"))
            menu.addItem_(self._menu_item("重启服务", "restartStaffDeck:"))
            dock_item = self._menu_item(self._dock_toggle_title(), "toggleDockIcon:")
            menu.addItem_(dock_item)
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            menu.addItem_(self._menu_item(f"关于 {APP_NAME}", "showAbout:"))
            menu.addItem_(self._menu_item(f"退出 {APP_NAME}", "quitStaffDeck:"))
            return menu, dock_item

        def _install_status_menu(self) -> None:
            self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
                AppKit.NSSquareStatusItemLength
            )
            button = self.status_item.button()
            if button is not None:
                status_icon = load_app_icon(18)
                if status_icon is not None:
                    status_icon.setTemplate_(False)
                    button.setImage_(status_icon)
                    button.setImagePosition_(AppKit.NSImageOnly)
                else:
                    button.setTitle_(APP_NAME)
                button.setToolTip_(APP_NAME)

            menu, self.status_dock_item = self._build_control_menu()
            self.status_item.setMenu_(menu)
            self.status_menu = menu

    app = AppKit.NSApplication.sharedApplication()
    # Regular：常规 GUI app，进 Dock、可激活
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    dock_icon = load_app_icon()
    if dock_icon is not None:
        app.setApplicationIconImage_(dock_icon)
    delegate = AppDelegate.alloc().init()
    # PyObjC 不总是按 Python 预期保留 delegate，模块级引用保证菜单和事件代理常驻。
    _MACOS_DELEGATE_REF = delegate
    app.setDelegate_(delegate)
    app.setMainMenu_(_create_macos_main_menu(AppKit, delegate))
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()
    return 0


def _run_windows_taskbar_app(cfg: dict, url: str) -> int:
    """Run the server behind a native window so StaffDeck owns a taskbar icon."""
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

    shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    large_icon = wintypes.HICON()
    small_icon = wintypes.HICON()
    shell32.ExtractIconExW(sys.executable, 0, ctypes.byref(large_icon), ctypes.byref(small_icon), 1)

    @WNDPROC
    def window_proc(hwnd, message, wparam, lparam):
        if _is_windows_restore_command(message, wparam):
            print(f"Taskbar activated; opening {APP_NAME} in the system default browser.")
            _open_browser(url + "/chat/")
            user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    class_name = "StaffDeckDesktopWindow"
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
        APP_NAME,
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
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args == ["--packaging-smoke"]:
        return _run_packaging_smoke()
    if raw_args and raw_args[0] == "setup":
        return _setup_network(raw_args[1:])
    _apply_network_config(raw_args)
    _redirect_logs_when_frozen()

    host = os.environ.get("ULTRARAG_HOST", "127.0.0.1")
    existing_url = _find_existing_app_url(host)
    if existing_url:
        print(f"{APP_NAME} 已在运行：{existing_url}/chat/")
        _open_browser(existing_url + "/chat/")
        return 0

    if _use_macos_dock_app() and not _acquire_macos_instance_lock():
        existing_url = _wait_for_existing_app_url(host)
        if existing_url:
            print(f"{APP_NAME} 正在运行：{existing_url}/chat/")
            _open_browser(existing_url + "/chat/")
        else:
            print(f"{APP_NAME} 已有实例正在启动，当前实例退出。")
        return 0

    # 时序：先选定端口并设 env，再 import uvicorn / 触发 app.* import。
    cfg = build_server_config()
    apply_runtime_env(cfg)
    url = cfg.get("public_url") or f"http://{cfg['host']}:{cfg['port']}"
    preload_server_app(cfg)

    if _use_macos_dock_app():
        return _run_macos_dock_app(cfg, url)

    if _use_windows_taskbar_app():
        return _run_windows_taskbar_app(cfg, url)

    if not _env_flag("STAFFDECK_HEADLESS"):
        print(f"{APP_NAME} 启动中，就绪后将打开：{url}/chat/")
        threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()
    else:
        print(f"{APP_NAME} headless 启动中：{url}")
    _serve(cfg)
    return 0


def run_frozen_safe_main() -> int:
    import multiprocessing

    multiprocessing.freeze_support()
    return main()


if __name__ == "__main__":
    sys.exit(run_frozen_safe_main())
