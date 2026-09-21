import base64
import json

import pytest

import desktop_launcher


def test_frozen_safe_main_calls_freeze_support_before_main(monkeypatch) -> None:
    import multiprocessing

    calls: list[str] = []
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: calls.append("freeze"))
    monkeypatch.setattr(desktop_launcher, "main", lambda: calls.append("main") or 7)

    assert desktop_launcher.run_frozen_safe_main() == 7
    assert calls == ["freeze", "main"]


def test_packaging_smoke_checks_lark_sdk_metadata_and_modules(monkeypatch, capsys) -> None:
    imported: list[str] = []
    modules = {
        module_name: type("FakeModule", (), {symbol_name: object()})
        for module_name, symbol_name in desktop_launcher.LARK_PACKAGING_SMOKE_IMPORTS
    }
    monkeypatch.setattr(desktop_launcher.importlib_metadata, "version", lambda name: "1.2.0")
    monkeypatch.setattr(
        desktop_launcher.importlib,
        "import_module",
        lambda name: imported.append(name) or modules[name],
    )

    assert desktop_launcher.main(["--packaging-smoke"]) == 0
    assert imported == [
        module_name for module_name, _symbol_name in desktop_launcher.LARK_PACKAGING_SMOKE_IMPORTS
    ]
    assert "lark-channel-sdk==1.2.0" in capsys.readouterr().out


def test_packaging_smoke_rejects_wrong_lark_sdk_version(monkeypatch) -> None:
    monkeypatch.setattr(desktop_launcher.importlib_metadata, "version", lambda name: "1.2.1")

    with pytest.raises(RuntimeError, match="must be exactly 1.2.0, got 1.2.1"):
        desktop_launcher.main(["--packaging-smoke"])


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("http://127.0.0.1:5173/workspace", False),
        ("http://127.0.0.1:5174/workspace", True),
        ("https://github.com/OpenBMB/StaffDeck/releases", True),
        ("http://127.0.0.1:invalid/workspace", False),
        ("staffdeck://open", False),
        ("not-a-url", False),
    ],
)
def test_external_web_url_detection(target: str, expected: bool) -> None:
    assert desktop_launcher._is_external_web_url(target, "http://127.0.0.1:5173") is expected


def _clear_port_env(monkeypatch) -> None:
    monkeypatch.delenv("ULTRARAG_PORT", raising=False)
    monkeypatch.delenv("ULTRARAG_PORT_RANGE_START", raising=False)
    monkeypatch.delenv("ULTRARAG_PORT_RANGE_END", raising=False)


def test_build_server_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ULTRARAG_HOST", raising=False)
    _clear_port_env(monkeypatch)
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, _port: False)
    cfg = desktop_launcher.build_server_config()
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 5173
    assert cfg["app"] == "single_port_app:app"


def test_setup_network_saves_local_mode(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)

    assert desktop_launcher._setup_network(["--mode", "local", "--port", "5180"]) == 0
    assert (tmp_path / "network.json").read_text(encoding="utf-8") == (
        '{\n  "mode": "local",\n  "host": "127.0.0.1",\n  "port": 5180,\n  "public_url": ""\n}\n'
    )


def test_apply_network_config_uses_persisted_lan_mode(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)
    desktop_launcher._save_network_config("lan", "", 5190)
    monkeypatch.delenv("ULTRARAG_HOST", raising=False)
    monkeypatch.delenv("ULTRARAG_PORT", raising=False)

    desktop_launcher._apply_network_config([])

    assert desktop_launcher.os.environ["ULTRARAG_HOST"] == "0.0.0.0"
    assert desktop_launcher.os.environ["ULTRARAG_PORT"] == "5190"


def test_apply_network_config_preserves_environment_overrides(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)
    desktop_launcher._save_network_config("lan", "", 5190)
    monkeypatch.setenv("ULTRARAG_HOST", "0.0.0.0")
    monkeypatch.setenv("ULTRARAG_PORT", "6200")

    desktop_launcher._apply_network_config([])

    assert desktop_launcher.os.environ["ULTRARAG_HOST"] == "0.0.0.0"
    assert desktop_launcher.os.environ["ULTRARAG_PORT"] == "6200"


def test_public_mode_uses_inferred_public_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop_launcher, "_infer_public_url", lambda port: f"http://203.0.113.9:{port}")

    assert desktop_launcher._setup_network(["--mode", "public", "--port", "5173"]) == 0
    assert (tmp_path / "network.json").read_text(encoding="utf-8") == (
        '{\n  "mode": "public",\n  "host": "0.0.0.0",\n  "port": 5173,\n  "public_url": "http://203.0.113.9:5173"\n}\n'
    )


def test_public_mode_requires_public_url_when_inference_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(desktop_launcher, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop_launcher, "_infer_public_url", lambda _port: "")
    monkeypatch.setattr(desktop_launcher.sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit, match="公网模式必须提供 --public-url"):
        desktop_launcher._setup_network(["--mode", "public", "--port", "5173"])


def test_public_url_does_not_redirect_backend_tool_calls(monkeypatch) -> None:
    monkeypatch.delenv("TOOL_BASE_URL", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    desktop_launcher.apply_runtime_env(
        {"host": "0.0.0.0", "port": 5173, "public_url": "https://staff.example.com"}
    )

    assert desktop_launcher.os.environ["TOOL_BASE_URL"] == "http://127.0.0.1:5173"
    assert "https://staff.example.com" in desktop_launcher.os.environ["CORS_ORIGINS"]


def test_build_server_config_env_override(monkeypatch) -> None:
    _clear_port_env(monkeypatch)
    monkeypatch.setenv("ULTRARAG_PORT", "6000")
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, _port: False)
    cfg = desktop_launcher.build_server_config()
    assert cfg["port"] == 6000


def test_build_server_config_uses_next_port_in_range(monkeypatch) -> None:
    _clear_port_env(monkeypatch)
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, port: port == 5173)
    cfg = desktop_launcher.build_server_config()
    assert cfg["port"] == 5174


def test_build_server_config_honors_custom_port_range(monkeypatch) -> None:
    _clear_port_env(monkeypatch)
    monkeypatch.setenv("ULTRARAG_PORT_RANGE_START", "6200")
    monkeypatch.setenv("ULTRARAG_PORT_RANGE_END", "6202")
    monkeypatch.setattr(desktop_launcher, "port_in_use", lambda _host, port: port in {6200, 6201})
    cfg = desktop_launcher.build_server_config()
    assert cfg["port"] == 6202


def test_explicit_port_is_tried_before_range(monkeypatch) -> None:
    _clear_port_env(monkeypatch)
    monkeypatch.setenv("ULTRARAG_PORT", "7000")
    monkeypatch.setenv("ULTRARAG_PORT_RANGE_START", "5173")
    monkeypatch.setenv("ULTRARAG_PORT_RANGE_END", "5174")
    checked_ports = []

    def fake_port_in_use(_host, port):
        checked_ports.append(port)
        return port == 7000

    monkeypatch.setattr(desktop_launcher, "port_in_use", fake_port_in_use)
    cfg = desktop_launcher.build_server_config()
    assert checked_ports == [7000, 5173]
    assert cfg["port"] == 5173


def test_port_in_use_false_for_unused_port() -> None:
    assert desktop_launcher.port_in_use("127.0.0.1", 59999) is False


def test_health_requires_staffdeck_marker(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload: bytes):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return self.payload

    def fake_urlopen(url, timeout):
        assert url == "http://127.0.0.1:5173/api/health"
        assert timeout == 1
        return FakeResponse(b'{"status":"ok","app":"StaffDeck"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert desktop_launcher._health_ok("http://127.0.0.1:5173") is True


def test_health_rejects_other_local_service(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())
    assert desktop_launcher._health_ok("http://127.0.0.1:5175") is False


def test_preload_server_app_imports_reference_on_calling_thread(monkeypatch) -> None:
    app = object()

    class FakeModule:
        pass

    module = FakeModule()
    module.app = app
    monkeypatch.setattr(desktop_launcher.importlib, "import_module", lambda name: module)
    cfg = {"app": "single_port_app:app"}

    desktop_launcher.preload_server_app(cfg)

    assert cfg["app"] is app


def test_windows_taskbar_app_only_used_for_frozen_windows(monkeypatch) -> None:
    monkeypatch.delenv("STAFFDECK_HEADLESS", raising=False)
    monkeypatch.setattr(desktop_launcher.sys, "platform", "win32")
    monkeypatch.delattr(desktop_launcher.sys, "frozen", raising=False)
    assert desktop_launcher._use_windows_taskbar_app() is False

    monkeypatch.setattr(desktop_launcher.sys, "frozen", True, raising=False)
    assert desktop_launcher._use_windows_taskbar_app() is True


def test_windows_taskbar_app_disabled_in_headless_mode(monkeypatch) -> None:
    monkeypatch.setattr(desktop_launcher.sys, "platform", "win32")
    monkeypatch.setattr(desktop_launcher.sys, "frozen", True, raising=False)
    monkeypatch.setenv("STAFFDECK_HEADLESS", "1")
    assert desktop_launcher._use_windows_taskbar_app() is False


def test_macos_dock_app_disabled_in_headless_mode(monkeypatch) -> None:
    monkeypatch.setattr(desktop_launcher.sys, "platform", "darwin")
    monkeypatch.setattr(desktop_launcher.sys, "frozen", True, raising=False)
    monkeypatch.setenv("STAFFDECK_HEADLESS", "1")
    assert desktop_launcher._use_macos_dock_app() is False


def test_windows_restore_command_detection() -> None:
    assert desktop_launcher._is_windows_restore_command(0x0112, 0xF120) is True
    assert desktop_launcher._is_windows_restore_command(0x0112, 0xF122) is True
    assert desktop_launcher._is_windows_restore_command(0x0112, 0xF020) is False
    assert desktop_launcher._is_windows_restore_command(0x0002, 0xF120) is False


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (1280, 800, (360, 768, 660, 32)),
        (900, 600, (360, 568, 280, 32)),
        (60, 8, (60, 0, 0, 8)),
    ],
)
def test_macos_drag_region_stays_on_safe_top_edge(
    width: float,
    height: float,
    expected: tuple[float, float, float, float],
) -> None:
    assert desktop_launcher._macos_drag_region_frame(width, height) == expected


@pytest.mark.parametrize(
    ("x", "y", "expected"),
    [
        (360, 768, True),
        (1019, 799, True),
        (359, 795, False),
        (500, 767, False),
        (1020, 795, False),
    ],
)
def test_macos_drag_region_excludes_traffic_lights_and_web_controls(
    x: float,
    y: float,
    expected: bool,
) -> None:
    assert desktop_launcher._point_is_in_macos_drag_region(x, y, 1280, 800) is expected


def test_macos_window_embeds_local_ui() -> None:
    events: dict[str, object] = {}

    class FakeContentView:
        def bounds(self):
            return (0, 0, 1280, 800)

    class FakeWindow:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithContentRect_styleMask_backing_defer_(self, frame, style, backing, defer):
            events["window_init"] = (frame, style, backing, defer)
            events["window"] = self
            return self

        def setTitle_(self, title):
            events["title"] = title

        def setTitleVisibility_(self, visibility):
            events["title_visibility"] = visibility

        def setTitlebarAppearsTransparent_(self, transparent):
            events["titlebar_transparent"] = transparent

        def setTitlebarSeparatorStyle_(self, style):
            events["titlebar_separator_style"] = style

        def setMinSize_(self, size):
            events["min_size"] = size

        def setReleasedWhenClosed_(self, released):
            events["released_when_closed"] = released

        def center(self):
            events["centered"] = True

        def contentView(self):
            return events.get("content_view", FakeContentView())

        def setContentView_(self, view):
            events["content_view"] = view

        def makeKeyAndOrderFront_(self, sender):
            events["ordered_front"] = sender

        def performZoom_(self, sender):
            events["zoom_sender"] = sender

        def performWindowDragWithEvent_(self, event):
            events["drag_event"] = event

        def sendEvent_(self, event):
            events["forwarded_event"] = event

    class FakeWebView:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithFrame_(self, frame):
            events["webview_frame"] = frame
            return self

        def setAutoresizingMask_(self, mask):
            events["autoresizing_mask"] = mask

        def bounds(self):
            return type(
                "Bounds",
                (),
                {"size": type("Size", (), {"width": 1280, "height": 800})()},
            )()

        def loadRequest_(self, request):
            events["request"] = request

    class FakeURL:
        @staticmethod
        def URLWithString_(target):
            return f"url:{target}"

    class FakeRequest:
        @staticmethod
        def requestWithURL_(url):
            return f"request:{url}"

    class FakeAppKit:
        NSWindow = FakeWindow
        NSEventTypeLeftMouseDown = 1
        NSWindowStyleMaskTitled = 1
        NSWindowStyleMaskClosable = 2
        NSWindowStyleMaskMiniaturizable = 4
        NSWindowStyleMaskResizable = 8
        NSWindowStyleMaskFullSizeContentView = 16
        NSWindowTitleHidden = 1
        NSWindowTitlebarSeparatorStyleNone = 0
        NSBackingStoreBuffered = 2
        NSViewWidthSizable = 2
        NSViewHeightSizable = 16

        @staticmethod
        def NSMakeRect(x, y, width, height):
            return (x, y, width, height)

        @staticmethod
        def NSMakeSize(width, height):
            return (width, height)

    class FakeFoundation:
        NSURL = FakeURL
        NSURLRequest = FakeRequest

    class FakeWebKit:
        WKWebView = FakeWebView

    original_window_class = desktop_launcher._MACOS_WINDOW_CLASS
    desktop_launcher._MACOS_WINDOW_CLASS = None
    try:
        window, webview = desktop_launcher._create_macos_webview_window(
            FakeAppKit,
            FakeFoundation,
            FakeWebKit,
            "http://127.0.0.1:5173/chat/",
        )
    finally:
        desktop_launcher._MACOS_WINDOW_CLASS = original_window_class

    assert isinstance(window, FakeWindow)
    assert isinstance(webview, FakeWebView)
    assert webview is events["content_view"]
    assert events["request"] == "request:url:http://127.0.0.1:5173/chat/"
    assert events["title"] == "StaffDeck"
    assert events["window_init"][1] & FakeAppKit.NSWindowStyleMaskFullSizeContentView
    assert events["title_visibility"] == FakeAppKit.NSWindowTitleHidden
    assert events["titlebar_transparent"] is True
    assert events["titlebar_separator_style"] == FakeAppKit.NSWindowTitlebarSeparatorStyleNone
    assert events["min_size"] == (900, 600)
    assert events["released_when_closed"] is False
    assert events["centered"] is True

    point = type("Point", (), {"x": 500, "y": 795})()
    drag_event = type(
        "Event",
        (),
        {
            "type": lambda self: FakeAppKit.NSEventTypeLeftMouseDown,
            "locationInWindow": lambda self: point,
            "clickCount": lambda self: 1,
        },
    )()
    window.sendEvent_(drag_event)
    assert events["drag_event"] is drag_event

    zoom_event = type(
        "Event",
        (),
        {
            "type": lambda self: FakeAppKit.NSEventTypeLeftMouseDown,
            "locationInWindow": lambda self: point,
            "clickCount": lambda self: 2,
        },
    )()
    window.sendEvent_(zoom_event)
    assert events["zoom_sender"] is window

    web_control_point = type("Point", (), {"x": 100, "y": 795})()
    web_event = type(
        "Event",
        (),
        {
            "type": lambda self: FakeAppKit.NSEventTypeLeftMouseDown,
            "locationInWindow": lambda self: web_control_point,
            "clickCount": lambda self: 1,
        },
    )()
    window.sendEvent_(web_event)
    assert events["forwarded_event"] is web_event


class _FakeDownloadMessage:
    """Minimal WKScriptMessage stand-in: an ObjC-dictionary-like body plus its webview."""

    def __init__(self, payload, webview, body_override=None):
        self._payload = payload
        self._webview = webview
        self._body_override = body_override

    def body(self):
        if self._body_override is not None:
            return self._body_override
        # WebKit 交给 Python 的是 ObjC 字典代理，用自定义映射模拟「不是 dict」这一点。
        return _ObjCDictionaryLike(self._payload)

    def webView(self):
        return self._webview


class _ObjCDictionaryLike:
    def __init__(self, payload):
        self._payload = payload

    def __iter__(self):
        return iter(self._payload)

    def __getitem__(self, key):
        if key not in self._payload:
            raise KeyError(key)
        return self._payload[key]

    def keys(self):
        return self._payload.keys()


class _FakeDownloadWebView:
    def __init__(self):
        self.scripts: list[str] = []

    def evaluateJavaScript_completionHandler_(self, script, handler):
        self.scripts.append(script)
        if handler is not None:
            handler(script, None)


class _FakeDownloadHandlerAppKit:
    """Fake AppKit whose NSObject is enough for PyObjC-less handler construction."""

    class NSObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    NSSavePanel = None
    NSModalResponseOK = 1


@pytest.fixture
def download_handler(monkeypatch):
    monkeypatch.setattr(desktop_launcher, "_MACOS_DOWNLOAD_HANDLER_CLASS", None)
    handler_class = desktop_launcher._macos_download_message_handler_class(
        _FakeDownloadHandlerAppKit
    )
    assert handler_class is not None
    return handler_class.alloc().init()


def _drive_download(handler, webview, transfer_id="t1", name="报告.xlsx", chunks=()):
    handler.userContentController_didReceiveScriptMessage_(
        None, _FakeDownloadMessage({"phase": "begin", "id": transfer_id, "name": name, "size": 0}, webview)
    )
    for chunk in chunks:
        handler.userContentController_didReceiveScriptMessage_(
            None, _FakeDownloadMessage({"phase": "chunk", "id": transfer_id, "data": chunk}, webview)
        )
    handler.userContentController_didReceiveScriptMessage_(
        None, _FakeDownloadMessage({"phase": "end", "id": transfer_id}, webview)
    )


def _reply_payloads(webview) -> list[dict]:
    replies = []
    for script in webview.scripts:
        json_start = script.find("(")
        replies.append(json.loads(script[json_start + 1 :].rstrip(")")))
    return replies


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("报告.xlsx", "报告.xlsx"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("hidden\u0001 name.bin", "hidden name.bin"),
        (".bashrc", "bashrc"),
        ("", "download"),
        ("/", "download"),
        ("x" * 400, "x" * desktop_launcher.MACOS_DOWNLOAD_NAME_LIMIT),
    ],
)
def test_download_name_sanitizing(raw: str, expected: str) -> None:
    assert desktop_launcher._sanitize_download_name(raw) == expected


def test_download_chunk_size_decodes_standalone() -> None:
    # 前端按固定字节数切片；每片 base64 都要能独立解码，因此必须是 3 的倍数。
    assert desktop_launcher.MACOS_DOWNLOAD_CHUNK_BYTES % 3 == 0


def test_download_transfers_reassemble_chunks() -> None:
    transfers = desktop_launcher._MacosDownloadTransfers()

    assert transfers.begin("t1", "报告.bin", 3) == ""
    assert transfers.append("t1", base64.b64encode(b"abc").decode()) == ""
    assert transfers.append("t1", base64.b64encode(b"def").decode()) == ""
    assert transfers.take("t1") == ("报告.bin", b"abcdef")
    assert transfers.pending_count() == 0


def test_download_transfers_reject_unknown_and_corrupt_input() -> None:
    transfers = desktop_launcher._MacosDownloadTransfers()

    assert "失效" in transfers.append("missing", base64.b64encode(b"x").decode())
    assert transfers.begin("t2", "x.bin", 1) == ""
    assert "损坏" in transfers.append("t2", "not-base64!!")
    # 数据损坏的传输会被丢弃，避免半个文件落到磁盘上
    assert transfers.take("t2") is None


def test_download_transfers_reject_declared_oversize() -> None:
    transfers = desktop_launcher._MacosDownloadTransfers(max_bytes=8)

    assert "超过" in transfers.begin("big", "x.bin", 9)
    assert transfers.pending_count() == 0


def test_download_transfers_reject_actual_oversize() -> None:
    transfers = desktop_launcher._MacosDownloadTransfers(max_bytes=8)

    assert transfers.begin("big", "x.bin", 4) == ""
    assert "超过" in transfers.append("big", base64.b64encode(b"123456789").decode())
    assert transfers.take("big") is None


def test_download_transfers_limit_concurrent_saves() -> None:
    transfers = desktop_launcher._MacosDownloadTransfers(max_pending=1)

    assert transfers.begin("first", "a.bin", 1) == ""
    assert "过多" in transfers.begin("second", "b.bin", 1)
    assert transfers.pending_count() == 1


def test_download_transfers_restart_reused_transfer_id() -> None:
    transfers = desktop_launcher._MacosDownloadTransfers()

    assert transfers.begin("t1", "first.bin", 0) == ""
    assert transfers.append("t1", base64.b64encode(b"first").decode()) == ""
    assert transfers.begin("t1", "second.bin", 0) == ""
    assert transfers.append("t1", base64.b64encode(b"second").decode()) == ""
    assert transfers.take("t1") == ("second.bin", b"second")


def test_download_message_payload_handles_objc_dictionary() -> None:
    body = _ObjCDictionaryLike({"phase": "begin", "id": "x"})

    assert desktop_launcher._macos_download_payload(body) == {"phase": "begin", "id": "x"}
    assert desktop_launcher._macos_download_payload({"phase": "end"}) == {"phase": "end"}
    assert desktop_launcher._macos_download_payload("nope") == {}


def test_download_reply_script_escapes_payload() -> None:
    script = desktop_launcher._macos_download_reply_script(
        {"id": "t1", "status": "saved", "path": "/tmp/报告.xlsx"}
    )

    assert script.startswith("window.__staffdeckDownloadResult && window.__staffdeckDownloadResult(")
    assert "\\u62a5\\u544a" in script
    assert _reply_payloads(type("W", (), {"scripts": [script]})())[0]["status"] == "saved"


def _fake_save_panel_appkit(response, path="/tmp/out.bin"):
    events: dict[str, object] = {}

    class FakePanel:
        def setTitle_(self, title):
            events["title"] = title

        def setNameFieldStringValue_(self, name):
            events["name"] = name

        def setCanCreateDirectories_(self, value):
            events["can_create_directories"] = value

        def runModal(self):
            return response

        def URL(self):
            return None if path is None else type("URL", (), {"path": lambda self: path})()

    class FakeSavePanel:
        @staticmethod
        def savePanel():
            return FakePanel()

    class FakeAppKit:
        NSModalResponseOK = 1
        NSSavePanel = FakeSavePanel

    return FakeAppKit, events


def test_save_panel_returns_chosen_path() -> None:
    appkit, events = _fake_save_panel_appkit(1, "/tmp/chosen/报告.xlsx")

    assert desktop_launcher._macos_choose_download_path(appkit, "报告.xlsx") == "/tmp/chosen/报告.xlsx"
    assert events["name"] == "报告.xlsx"
    assert events["can_create_directories"] is True
    assert "StaffDeck" in str(events["title"])


def test_save_panel_cancel_and_missing_url_return_none() -> None:
    cancelled, _ = _fake_save_panel_appkit(0)
    empty_url, _ = _fake_save_panel_appkit(1, None)

    assert desktop_launcher._macos_choose_download_path(cancelled, "x.bin") is None
    assert desktop_launcher._macos_choose_download_path(empty_url, "x.bin") is None


def test_download_handler_writes_file_and_reports_saved(download_handler, monkeypatch, tmp_path) -> None:
    target = tmp_path / "报告.xlsx"
    monkeypatch.setattr(
        desktop_launcher, "_macos_choose_download_path", lambda _appkit, _name: str(target)
    )
    webview = _FakeDownloadWebView()

    _drive_download(
        download_handler,
        webview,
        chunks=[base64.b64encode(b"hello ").decode(), base64.b64encode(b"world").decode()],
    )

    assert target.read_bytes() == b"hello world"
    assert _reply_payloads(webview) == [
        {"id": "t1", "status": "saved", "path": str(target)},
    ]


def test_download_handler_reports_cancelled_without_writing(download_handler, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(desktop_launcher, "_macos_choose_download_path", lambda _appkit, _name: None)
    webview = _FakeDownloadWebView()

    _drive_download(download_handler, webview, chunks=[base64.b64encode(b"data").decode()])

    assert list(tmp_path.iterdir()) == []
    assert _reply_payloads(webview) == [{"id": "t1", "status": "cancelled"}]


def test_download_handler_reports_write_failure(download_handler, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        desktop_launcher,
        "_macos_choose_download_path",
        lambda _appkit, _name: str(tmp_path / "missing" / "out.bin"),
    )
    webview = _FakeDownloadWebView()

    _drive_download(download_handler, webview, chunks=[base64.b64encode(b"data").decode()])

    [reply] = _reply_payloads(webview)
    assert reply["status"] == "error"
    assert "写入文件失败" in reply["message"]


def test_download_handler_answers_unknown_transfer_and_bad_payload(download_handler) -> None:
    webview = _FakeDownloadWebView()

    download_handler.userContentController_didReceiveScriptMessage_(
        None, _FakeDownloadMessage({"phase": "end", "id": "ghost"}, webview)
    )
    download_handler.userContentController_didReceiveScriptMessage_(
        None, _FakeDownloadMessage(None, webview, body_override="not-a-dictionary")
    )
    download_handler.userContentController_didReceiveScriptMessage_(
        None, _FakeDownloadMessage({"phase": "begin", "name": "no-id.bin"}, webview)
    )

    [reply] = _reply_payloads(webview)
    assert reply["id"] == "ghost"
    assert reply["status"] == "error"


def test_download_handler_ignores_unknown_phase(download_handler) -> None:
    webview = _FakeDownloadWebView()

    download_handler.userContentController_didReceiveScriptMessage_(
        None, _FakeDownloadMessage({"phase": "junk", "id": "t1"}, webview)
    )

    assert webview.scripts == []


def test_download_handler_class_requires_nsobject(monkeypatch) -> None:
    class NoNSObjectAppKit:
        pass

    monkeypatch.setattr(desktop_launcher, "_MACOS_DOWNLOAD_HANDLER_CLASS", None)

    assert desktop_launcher._macos_download_message_handler_class(NoNSObjectAppKit) is None


def test_webview_configuration_registers_download_bridge() -> None:
    events: dict[str, object] = {}

    class FakeController:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def addScriptMessageHandler_name_(self, handler, name):
            events["handler"] = handler
            events["handler_name"] = name

    class FakeConfiguration:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setUserContentController_(self, controller):
            events["controller"] = controller

    class FakeWebKit:
        WKUserContentController = FakeController
        WKWebViewConfiguration = FakeConfiguration

    handler = object()
    configuration = desktop_launcher._create_macos_webview_configuration(FakeWebKit, handler)

    assert isinstance(configuration, FakeConfiguration)
    assert events["handler"] is handler
    assert events["handler_name"] == desktop_launcher.MACOS_DOWNLOAD_HANDLER_NAME
    assert isinstance(events["controller"], FakeController)


def test_webview_configuration_is_skipped_without_handler_or_classes() -> None:
    class FakeWebKit:
        pass

    assert desktop_launcher._create_macos_webview_configuration(FakeWebKit, object()) is None
    assert desktop_launcher._create_macos_webview_configuration(_FakeDownloadHandlerAppKit, None) is None


def _webview_fakes(*, with_configuration: bool):
    events: dict[str, object] = {}

    class FakeContentView:
        def bounds(self):
            return (0, 0, 1280, 800)

    class FakeWindow:
        def contentView(self):
            return FakeContentView()

    class FakeWebView:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithFrame_configuration_(self, frame, configuration):
            events["configured"] = (frame, configuration)
            return self

        def initWithFrame_(self, frame):
            events["plain"] = frame
            return self

    class FakeController:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def addScriptMessageHandler_name_(self, handler, name):
            events["registered"] = (handler, name)

    class FakeConfiguration:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setUserContentController_(self, controller):
            events["controller"] = controller

    class FakeWebKit:
        WKWebView = FakeWebView

    if with_configuration:
        FakeWebKit.WKUserContentController = FakeController
        FakeWebKit.WKWebViewConfiguration = FakeConfiguration

    return FakeWindow(), FakeWebKit, events


def test_macos_webview_uses_download_bridge_configuration() -> None:
    window, webkit, events = _webview_fakes(with_configuration=True)

    webview = desktop_launcher._create_macos_webview(
        _FakeDownloadHandlerAppKit, webkit, window
    )

    frame, configuration = events["configured"]
    assert frame == (0, 0, 1280, 800)
    assert configuration is events["controller"] or configuration is not None
    assert events["registered"][1] == desktop_launcher.MACOS_DOWNLOAD_HANDLER_NAME
    assert "plain" not in events
    _ = webview


def test_macos_webview_falls_back_without_download_bridge() -> None:
    window, webkit, events = _webview_fakes(with_configuration=False)

    desktop_launcher._create_macos_webview(_FakeDownloadHandlerAppKit, webkit, window)

    assert events["plain"] == (0, 0, 1280, 800)
    assert "configured" not in events



def test_macos_main_menu_routes_edit_shortcuts_through_responder_chain() -> None:
    command = 1 << 20
    option = 1 << 19

    class FakeMenuItem:
        def __init__(self, title="", action=None, key="", separator=False):
            self.title = title
            self.action = action
            self.key = key
            self.separator = separator
            self.target = None
            self.modifiers = command
            self.submenu = None

        @classmethod
        def alloc(cls):
            return cls()

        @classmethod
        def separatorItem(cls):
            return cls(separator=True)

        def initWithTitle_action_keyEquivalent_(self, title, action, key):
            self.title = title
            self.action = action
            self.key = key
            return self

        def setTarget_(self, target):
            self.target = target

        def setSubmenu_(self, submenu):
            self.submenu = submenu

        def setKeyEquivalentModifierMask_(self, modifiers):
            self.modifiers = modifiers

    class FakeMenu:
        def __init__(self):
            self.title = ""
            self.items = []

        @classmethod
        def alloc(cls):
            return cls()

        def initWithTitle_(self, title):
            self.title = title
            return self

        def addItem_(self, item):
            self.items.append(item)

    class FakeAppKit:
        NSMenu = FakeMenu
        NSMenuItem = FakeMenuItem
        NSEventModifierFlagCommand = command
        NSEventModifierFlagOption = option

    delegate = object()
    main_menu = desktop_launcher._create_macos_main_menu(FakeAppKit, delegate)

    assert [item.title for item in main_menu.items] == ["StaffDeck", "编辑"]

    app_items = [item for item in main_menu.items[0].submenu.items if not item.separator]
    assert [(item.title, item.action, item.key) for item in app_items] == [
        ("关于 StaffDeck", "showAbout:", ""),
        ("隐藏 StaffDeck", "hide:", "h"),
        ("隐藏其他", "hideOtherApplications:", "h"),
        ("全部显示", "unhideAllApplications:", ""),
        ("退出 StaffDeck", "quitStaffDeck:", "q"),
    ]
    assert app_items[0].target is delegate
    assert app_items[2].modifiers == command | option
    assert app_items[-1].target is delegate

    edit_items = [item for item in main_menu.items[1].submenu.items if not item.separator]
    assert [(item.action, item.key) for item in edit_items] == [
        ("undo:", "z"),
        ("redo:", "Z"),
        ("cut:", "x"),
        ("copy:", "c"),
        ("paste:", "v"),
        ("selectAll:", "a"),
    ]
    assert all(item.target is None for item in edit_items)
    assert all(item.modifiers == command for item in edit_items)


def test_frozen_server_disables_api_access_logging(monkeypatch) -> None:
    import uvicorn

    calls = []
    monkeypatch.setattr(desktop_launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    desktop_launcher._serve({"app": "single_port_app:app", "host": "127.0.0.1", "port": 5173})

    assert calls[0][1]["access_log"] is False
    assert calls[0][1]["log_config"] is None
