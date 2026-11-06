"""lark-cli 进程执行：每用户 HOME、凭据注入、最小环境、限时限量。

登录态与密钥全部落在 ``user_data_dir()/lark_cli/homes/<tenant>/<user>/``
（探针已验证 lark-cli 会把配置写进 ``$HOME/.lark-cli/`` 并把 app secret
加密进 ``$HOME/Library/Application Support/lark-cli/``，master key 在无钥匙
串环境自动降级为本地文件），因此按 HOME 隔离即可实现按用户隔离。
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app import paths

_MAX_OUTPUT_CHARS = 120_000
_SAFE_KEY_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")


class LarkCliRunError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LarkCliResult:
    exit_code: int
    stdout: str
    stderr: str
    envelope: dict[str, object] | None


def user_home_dir(tenant_id: str, user_key: str) -> Path:
    home = (
        paths.user_data_dir()
        / "lark_cli"
        / "homes"
        / _safe_component(tenant_id)
        / _safe_component(user_key)
    )
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(stat.S_IRWXU)
    return home


def configured_app_ids(home: Path) -> set[str]:
    """config.json 中已配置的全部 appId（CLI 支持多 app，匹配任意一个即视为已配置）。"""

    config_path = home / ".lark-cli" / "config.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    found: set[str] = set()
    apps = raw.get("apps") if isinstance(raw, dict) else None
    if isinstance(apps, list):
        for app in apps:
            if isinstance(app, dict) and str(app.get("appId") or "").strip():
                found.add(str(app["appId"]).strip())
    # 兼容旧的单 app 结构。
    if isinstance(raw, dict) and str(raw.get("appId") or "").strip():
        found.add(str(raw["appId"]).strip())
    return found


def ensure_configured(
    binary: Path,
    home: Path,
    *,
    app_id: str,
    app_secret: str,
    brand: str = "feishu",
) -> None:
    """幂等注入应用凭据；密钥经 stdin 传入，绝不进 argv 与日志。"""

    if app_id in configured_app_ids(home):
        return
    result = run_lark_cli(
        binary,
        home,
        ["config", "init", "--app-id", app_id, "--app-secret-stdin", "--brand", brand],
        stdin_text=app_secret,
        timeout_seconds=60.0,
        redact=(app_secret,),
    )
    if app_id not in configured_app_ids(home):
        detail = _first_error_message(result) or result.stderr.strip()[:500]
        raise LarkCliRunError(
            "LARK_CLI_CONFIG_INIT_FAILED",
            f"lark-cli 凭据初始化失败：{detail or '未知原因'}",
        )


def run_lark_cli(
    binary: Path,
    home: Path,
    argv: list[str],
    *,
    timeout_seconds: float,
    stdin_text: str | None = None,
    redact: tuple[str, ...] = (),
) -> LarkCliResult:
    env = process_environment(home)
    process = subprocess.Popen(
        [str(binary), *argv],
        cwd=str(home),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = process.communicate(input=stdin_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        stdout, stderr = process.communicate()
        raise LarkCliRunError(
            "LARK_CLI_TIMEOUT",
            f"lark-cli 命令超时（{timeout_seconds:.0f}s）已终止；可稍后重试。",
        ) from None
    stdout = _redact(stdout[:_MAX_OUTPUT_CHARS], redact)
    stderr = _redact(stderr[:_MAX_OUTPUT_CHARS], redact)
    return LarkCliResult(
        exit_code=int(process.returncode or 0),
        stdout=stdout,
        stderr=stderr,
        envelope=_parse_envelope(stdout) or _parse_envelope(stderr),
    )


def process_environment(home: Path) -> dict[str, str]:
    """lark-cli 子进程的最小环境（background.py 的长驻进程也复用）。"""

    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
    }
    if sys.platform == "win32":
        env["USERPROFILE"] = str(home)
        for key in ("SystemRoot", "TEMP", "TMP", "PATH"):
            value = os.environ.get(key)
            if value:
                env[key] = value
    return env


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    process.kill()


def _parse_envelope(text: str) -> dict[str, object] | None:
    """CLI 输出是一或多个 JSON 块（可能混有提示行）；取最后一个合法 object。"""

    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    last: dict[str, object] | None = None
    index = 0
    while index < len(stripped):
        brace = stripped.find("{", index)
        if brace < 0:
            break
        try:
            candidate, end = decoder.raw_decode(stripped, brace)
        except json.JSONDecodeError:
            index = brace + 1
            continue
        if isinstance(candidate, dict):
            last = candidate
        index = end
    return last


def _first_error_message(result: LarkCliResult) -> str | None:
    envelope = result.envelope
    if isinstance(envelope, dict):
        error = envelope.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return None


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _safe_component(value: str) -> str:
    text = _SAFE_KEY_PATTERN.sub("_", str(value or "").strip()) or "default"
    return text[:80]
