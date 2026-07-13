import desktop_launcher


def test_build_server_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ULTRARAG_HOST", raising=False)
    monkeypatch.delenv("ULTRARAG_PORT", raising=False)
    cfg = desktop_launcher.build_server_config()
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 5173
    assert cfg["app"] == "single_port_app:app"


def test_build_server_config_env_override(monkeypatch) -> None:
    monkeypatch.setenv("ULTRARAG_PORT", "6000")
    cfg = desktop_launcher.build_server_config()
    assert cfg["port"] == 6000


def test_port_in_use_false_for_unused_port() -> None:
    assert desktop_launcher.port_in_use("127.0.0.1", 59999) is False


def test_windows_taskbar_app_only_used_for_frozen_windows(monkeypatch) -> None:
    monkeypatch.setattr(desktop_launcher.sys, "platform", "win32")
    monkeypatch.delattr(desktop_launcher.sys, "frozen", raising=False)
    assert desktop_launcher._use_windows_taskbar_app() is False

    monkeypatch.setattr(desktop_launcher.sys, "frozen", True, raising=False)
    assert desktop_launcher._use_windows_taskbar_app() is True


def test_windows_restore_command_detection() -> None:
    assert desktop_launcher._is_windows_restore_command(0x0112, 0xF120) is True
    assert desktop_launcher._is_windows_restore_command(0x0112, 0xF122) is True
    assert desktop_launcher._is_windows_restore_command(0x0112, 0xF020) is False
    assert desktop_launcher._is_windows_restore_command(0x0002, 0xF120) is False
