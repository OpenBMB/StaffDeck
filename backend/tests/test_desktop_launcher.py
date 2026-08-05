import pytest

import desktop_launcher


def test_frozen_safe_main_calls_freeze_support_before_main(monkeypatch) -> None:
    import multiprocessing

    calls: list[str] = []
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: calls.append("freeze"))
    monkeypatch.setattr(desktop_launcher, "main", lambda: calls.append("main") or 7)

    assert desktop_launcher.run_frozen_safe_main() == 7
    assert calls == ["freeze", "main"]


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
            return self

        def setTitle_(self, title):
            events["title"] = title

        def setMinSize_(self, size):
            events["min_size"] = size

        def setReleasedWhenClosed_(self, released):
            events["released_when_closed"] = released

        def center(self):
            events["centered"] = True

        def contentView(self):
            return FakeContentView()

        def setContentView_(self, view):
            events["content_view"] = view

        def makeKeyAndOrderFront_(self, sender):
            events["ordered_front"] = sender

    class FakeWebView:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithFrame_(self, frame):
            events["webview_frame"] = frame
            return self

        def setAutoresizingMask_(self, mask):
            events["autoresizing_mask"] = mask

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
        NSWindowStyleMaskTitled = 1
        NSWindowStyleMaskClosable = 2
        NSWindowStyleMaskMiniaturizable = 4
        NSWindowStyleMaskResizable = 8
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

    window, webview = desktop_launcher._create_macos_webview_window(
        FakeAppKit,
        FakeFoundation,
        FakeWebKit,
        "http://127.0.0.1:5173/chat/",
    )

    assert isinstance(window, FakeWindow)
    assert isinstance(webview, FakeWebView)
    assert webview is events["content_view"]
    assert events["request"] == "request:url:http://127.0.0.1:5173/chat/"
    assert events["title"] == "StaffDeck"
    assert events["min_size"] == (900, 600)
    assert events["released_when_closed"] is False
    assert events["centered"] is True


def test_frozen_server_disables_api_access_logging(monkeypatch) -> None:
    import uvicorn

    calls = []
    monkeypatch.setattr(desktop_launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    desktop_launcher._serve({"app": "single_port_app:app", "host": "127.0.0.1", "port": 5173})

    assert calls[0][1]["access_log"] is False
    assert calls[0][1]["log_config"] is None
