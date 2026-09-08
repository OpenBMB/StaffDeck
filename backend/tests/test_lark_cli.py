from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.channels.crypto import encrypt_channel_secret
from app.config import Settings
from app.core.capability_manifest import (
    RESERVED_HARNESS_CAPABILITY_NAMES,
    _lark_cli_descriptor,
)
from app.db.models import ChannelBinding, ChatSession, HarnessInvocationRecord, Skill
from app.lark_cli import policy, runner, service


def _submit_skill() -> Skill:
    return Skill(
        tenant_id="t1",
        skill_id="sop-lark",
        name="飞书审批提交",
        content_json={
            "start_node_id": "n_submit",
            "nodes": [
                {
                    "node_id": "n_submit",
                    "name": "提交审批",
                    "instruction": "确认后提交",
                    "capability_refs": {"tool_ids": ["lark_cli"]},
                }
            ],
            "edges": [],
        },
    )


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def test_policy_allows_read_and_marks_dry_run() -> None:
    resolved = policy.resolve(
        ["approval", "approvals", "search", "--data", '{"keyword":"报销"}']
    )
    assert resolved.rule.action == "read"
    assert not resolved.is_side_effect_write

    dry = policy.resolve(
        [
            "approval",
            "instances",
            "create",
            "--data",
            '{"approval_code":"A","form":"[]"}',
            "--dry-run",
        ]
    )
    assert dry.rule.action == "gated_write"
    assert dry.is_dry_run and not dry.is_side_effect_write


@pytest.mark.parametrize(
    "argv,code",
    [
        (["api", "GET", "/open-apis/x"], "LARK_CLI_COMMAND_BLOCKED"),
        (["config", "show"], "LARK_CLI_COMMAND_BLOCKED"),
        (["im", "messages", "send", "--yes"], "LARK_CLI_YES_NOT_ALLOWED"),
        (["approval", "tasks", "query", "--data", "@/etc/passwd"], "LARK_CLI_FILE_REF_BLOCKED"),
        (["approval", "approvals", "get", "--as", "bot"], "LARK_CLI_IDENTITY_BLOCKED"),
        (
            ["approval", "instances", "create", "--data", "{}", "--yes"],
            "LARK_CLI_YES_NOT_ALLOWED",
        ),
        (
            ["approval", "instances", "create", "--data", "@/etc/passwd"],
            "LARK_CLI_FILE_REF_BLOCKED",
        ),
        (["approval", "instances", "create", "--data", "-"], "LARK_CLI_FILE_REF_BLOCKED"),
        (
            ["approval", "instances", "create", "--data", '{"uuid":"mine"}'],
            "LARK_CLI_UUID_NOT_ALLOWED",
        ),
        (
            ["approval", "instances", "create", "--data", "{}", "--as", "bot"],
            "LARK_CLI_IDENTITY_BLOCKED",
        ),
        (["auth", "login", "--output", "x"], "LARK_CLI_FLAG_BLOCKED"),
        (["auth", "qrcode", "--url", "https://x"], "LARK_CLI_COMMAND_BLOCKED"),
    ],
)
def test_policy_blocks(argv: list[str], code: str) -> None:
    with pytest.raises(policy.LarkCliPolicyError) as exc:
        policy.resolve(argv)
    assert exc.value.code == code


def test_policy_read_commands_accept_official_typed_flags() -> None:
    """真实案例：官方正确用法 `approvals get --approval-code X` 曾被白名单
    误拦，逼模型每个新对话都重复试错。读命令 flag 全放行。"""

    resolved = policy.resolve(
        ["approval", "approvals", "get", "--approval-code", "X", "--locale", "zh-CN"]
    )
    assert resolved.rule.action == "read" and not resolved.needs_risk_probe
    assert not resolved.is_side_effect_write


def test_policy_unknown_commands_become_risk_probe_candidates() -> None:
    """未登记命令交给动态 Risk 裁决；api/config 永不进入动态通道。"""

    for argv in (
        ["approval", "tasks", "approve", "--data", "{}"],
        ["approval", "instances", "cancel", "--data", "{}"],
        ["im", "messages", "list"],
        ["contact", "users", "get", "--user-id", "u1"],
    ):
        resolved = policy.resolve(argv)
        assert resolved.needs_risk_probe, argv
        assert resolved.rule.action == "read"
    with pytest.raises(policy.LarkCliPolicyError):
        policy.resolve(["api", "POST", "/open-apis/x"])
    with pytest.raises(policy.LarkCliPolicyError):
        policy.resolve(["config", "bind"])


def test_submission_digest_is_stable_and_ignores_uuid() -> None:
    base = {"approval_code": "A", "form": '[{"id":"w1","value":"x"}]'}
    digest = policy.submission_digest(dict(base))
    assert digest == policy.submission_digest({**base, "uuid": "anything"})
    assert digest != policy.submission_digest({**base, "form": "[]"})


def test_submission_digest_is_encoding_agnostic() -> None:
    """form 传数组与传等价 JSON 字符串必须得到同一摘要（真实线上翻车案例）。"""

    as_array = {"approval_code": "A", "form": [{"id": "w1", "value": "x"}]}
    as_string = {"approval_code": "A", "form": '[{"id": "w1", "value": "x"}]'}
    assert policy.submission_digest(as_array) == policy.submission_digest(as_string)


def test_wire_submission_data_serializes_form_to_string() -> None:
    wire = policy.wire_submission_data(
        {"approval_code": "A", "form": [{"id": "w1", "value": "x"}]}
    )
    assert isinstance(wire["form"], str)
    assert json.loads(wire["form"]) == [{"id": "w1", "value": "x"}]


def test_wire_form_dates_normalized_to_rfc3339() -> None:
    """定义详情把 date 控件展示为 YYYY-MM-DD hh:mm，模型照抄提交会被服务端以
    "start time format is not RFC3339" 拒绝（真实案例）——受信层必须兜底转换，
    含复合控件（leaveGroupV2）嵌套子控件。"""

    form = [
        {
            "id": "widgetLeaveGroupV2",
            "type": "leaveGroupV2",
            "value": [
                {"id": "t", "type": "radioV2", "value": "7678264870298471375"},
                {"id": "s", "type": "date", "value": "2026-08-27 00:00"},
                {"id": "r", "type": "textarea", "value": "StaffDeck测试"},
            ],
        }
    ]
    wire = policy.wire_submission_data({"approval_code": "A", "form": form})
    sent = json.loads(wire["form"])[0]["value"]
    expected = (
        datetime.fromisoformat("2026-08-27 00:00").astimezone().isoformat(timespec="seconds")
    )
    assert sent[1]["value"] == expected
    assert "T" in expected and expected != "2026-08-27 00:00"
    # 非日期控件不动；原始输入不被就地修改。
    assert sent[0]["value"] == "7678264870298471375"
    assert sent[2]["value"] == "StaffDeck测试"
    assert form[0]["value"][1]["value"] == "2026-08-27 00:00"


def test_wire_form_date_interval_normalized_and_unparseable_passthrough() -> None:
    wire = policy.wire_submission_data(
        {
            "form": [
                {
                    "id": "d",
                    "type": "dateInterval",
                    "value": {"start": "2026-08-27", "end": "2026-08-28", "interval": 24.0},
                }
            ]
        }
    )
    sent = json.loads(wire["form"])[0]["value"]
    assert "T" in sent["start"] and "T" in sent["end"]
    assert sent["interval"] == 24.0
    # 解析不了的值原样保留，交给服务端报错（错误制导会指回该控件）。
    wire2 = policy.wire_submission_data({"form": [{"id": "d", "type": "date", "value": "明天"}]})
    assert json.loads(wire2["form"])[0]["value"] == "明天"


def test_submission_digest_is_date_encoding_agnostic() -> None:
    """dry-run 写 YYYY-MM-DD hh:mm、提交自修正为 RFC3339 时摘要必须一致，
    否则确认闸会误杀合法的日期格式自修正。"""

    rfc = datetime.fromisoformat("2026-08-27 09:00").astimezone().isoformat(timespec="seconds")
    local_form = {"form": [{"id": "s", "type": "date", "value": "2026-08-27 09:00"}]}
    rfc_form = {"form": [{"id": "s", "type": "date", "value": rfc}]}
    assert policy.submission_digest(local_form) == policy.submission_digest(rfc_form)


def test_form_error_guidance_covers_observed_server_rejections() -> None:
    """真实翻车过的两类服务端拒绝必须有针对性制导（消息里都不含 'form' 字样）。"""

    widget = service._form_error_guidance(
        "审批定义中未找到表单控件, 请重新获取定义详情确认该控件是否存在. "
        "index= 0, ID= widgetLeaveGroupType.",
        "api",
    )
    assert "复合控件" in widget and "平铺" in widget
    rfc = service._form_error_guidance("start time format is not RFC3339", "api")
    assert "RFC3339" in rfc and "--dry-run" in rfc
    generic = service._form_error_guidance("Invalid parameter type in json: form", "invalid")
    assert "选项 key" in generic
    assert service._form_error_guidance("no scope", "permission_denied") == ""


def test_policy_allows_help_on_intermediate_prefix() -> None:
    resolved = policy.resolve(["approval", "instances", "--help"])
    assert resolved.is_help and not resolved.is_side_effect_write
    assert policy.resolve(["config", "--help"]).is_help
    with pytest.raises(policy.LarkCliPolicyError):
        policy.resolve(["api", "--help"])


def test_task_requirement_carries_current_time() -> None:
    from app.core.task_request_compiler import CapabilityManifest, TaskRequestCompiler
    from app.session.session_schema import PlannedTaskFrame

    requirement = TaskRequestCompiler().compile(
        PlannedTaskFrame(task_id="t", kind="conversation"),
        ChatSession(id="s", tenant_id="t1"),
        None,
        CapabilityManifest(),
    )
    assert requirement.current_time
    assert "T" in requirement.current_time and "周" in requirement.current_time


def test_logical_signature_only_for_real_submission() -> None:
    submit = ["approval", "instances", "create", "--data", '{"approval_code":"A"}']
    assert policy.logical_write_signature({"args": submit}) is not None
    assert policy.logical_write_signature({"args": [*submit, "--dry-run"]}) is None
    assert (
        policy.logical_write_signature(
            {"args": ["auth", "login", "--device-code", "d"]}
        )
        is None
    )
    assert policy.logical_write_signature({"args": ["auth", "status"]}) is None
    # --help 视为只读，不产生防重放签名。
    assert policy.logical_write_signature({"args": [*submit, "--help"]}) is None


# ---------------------------------------------------------------------------
# runner helpers
# ---------------------------------------------------------------------------


def test_parse_envelope_picks_last_json_object() -> None:
    text = 'OK: saved\n{"first": true}\nnoise\n{"ok": false, "error": {"message": "x"}}'
    parsed = runner._parse_envelope(text)
    assert parsed == {"ok": False, "error": {"message": "x"}}
    assert runner._parse_envelope("no json here") is None


def test_redact_removes_secret() -> None:
    assert "s3cret" not in runner._redact("token s3cret leaked", ("s3cret",))


def test_user_home_dir_isolated_per_user(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    home_a = runner.user_home_dir("tenant-1", "user/a")
    home_b = runner.user_home_dir("tenant-1", "user-b")
    assert home_a != home_b
    assert home_a.is_dir() and home_b.is_dir()
    if os.name == "posix":
        assert stat.S_IMODE(home_a.stat().st_mode) == 0o700


# ---------------------------------------------------------------------------
# service (fake binary end-to-end)
# ---------------------------------------------------------------------------

_FAKE_BINARY = r'''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
home = os.environ["HOME"]
if "--help" in args or "-h" in args:
    # 模拟真实 CLI：help 文本携带 Risk 分级与 --as 说明（动态裁决依据）。
    joined = " ".join(args)
    if "tasks approve" in joined or "instances cancel" in joined:
        risk = "write"
    else:
        risk = "read"
    print("Fake command help\n\nRisk: %s\n\nExecution:\n"
          "      --as string   identity type: user | bot" % risk)
    sys.exit(0)
if args[:2] == ["config", "init"] and "--new" in args:
    # 真实 CLI：阻塞到用户在浏览器完成创建；先输出 verification URL。
    print("Open to create app: https://open.feishu.cn/app/create?ticket=abc123", flush=True)
    import time
    time.sleep(30)
    sys.exit(0)
if args[:2] == ["config", "init"]:
    secret = sys.stdin.read()
    app_id = args[args.index("--app-id") + 1]
    directory = os.path.join(home, ".lark-cli")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "config.json"), "w") as fh:
        json.dump({"apps": [{"appId": app_id, "brand": "feishu"}]}, fh)
    print(json.dumps({"ok": True, "saw_secret": bool(secret)}))
    sys.exit(0)
if args[:3] == ["approval", "approvals", "get"] and "LEAVE_DEF" in args:
    # 供表单结构校验用的定义样例：复合控件子控件嵌套在 value 数组内，
    # 与真实「请假」定义同构。
    definition = [
        {"id": "widgetLeaveGroupV2", "type": "leaveGroupV2", "required": True, "value": [
            {"id": "widgetLeaveGroupType", "type": "radioV2",
             "option": [{"value": "7678264870298471375", "text": "事假"},
                        {"value": "7678264870520835028", "text": "病假"}]},
            {"id": "widgetLeaveGroupStartTime", "type": "date"},
            {"id": "widgetLeaveGroupEndTime", "type": "date"},
            {"id": "widgetLeaveGroupReason", "type": "textarea"},
        ]},
        {"id": "widgetRemark", "type": "textarea"},
    ]
    print(json.dumps({"ok": True, "data": {"form": json.dumps(definition)}}))
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    # 真实 CLI 的 auth status 信封没有 ok 字段。
    print(json.dumps({
        "appId": "cli_test_app",
        "identities": {"user": {"available": False, "status": "missing"}},
        "argv": args,
    }))
    sys.exit(0)
if any("trigger_error" in item for item in args):
    print(json.dumps({
        "ok": False,
        "error": {"type": "api", "subtype": "permission_denied",
                  "message": "no scope", "hint": "run auth login"},
    }))
    sys.exit(0)
print(json.dumps({"ok": True, "argv": args, "env_ci": os.environ.get("CI", "")}))
'''


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture()
def lark_env(monkeypatch, tmp_path: Path, db: Session):
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    binary = tmp_path / "fake-lark-cli"
    binary.write_text(_FAKE_BINARY, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(service, "ensure_lark_cli", lambda: binary)
    monkeypatch.setattr(
        service, "get_settings", lambda: Settings(lark_cli_enabled=True)
    )
    chat = ChatSession(id="sess-1", tenant_id="t1", user_id="u1", agent_id="agent-1")
    binding = ChannelBinding(
        tenant_id="t1",
        agent_id="agent-1",
        channel="feishu",
        status="active",
        credentials_enc=encrypt_channel_secret("app-secret-value"),
        config_json={"app_id": "cli_test_app"},
    )
    db.add(chat)
    db.add(binding)
    db.commit()
    db.refresh(chat)
    return {"db": db, "session": chat, "binary": binary}


@pytest.fixture()
def lark_env_unbound(monkeypatch, tmp_path: Path, db: Session):
    """无渠道绑定、无 settings 凭据的环境：验证对话内配置路径。"""

    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    binary = tmp_path / "fake-lark-cli"
    binary.write_text(_FAKE_BINARY, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(service, "ensure_lark_cli", lambda: binary)
    monkeypatch.setattr(
        service, "get_settings", lambda: Settings(lark_cli_enabled=True)
    )
    chat = ChatSession(id="sess-1", tenant_id="t1", user_id="u1", agent_id="agent-1")
    db.add(chat)
    db.commit()
    db.refresh(chat)
    return {"db": db, "session": chat, "binary": binary}


def _invoke(env, arguments, *, active_skill=None, active_step_id=None):
    return service.invoke_lark_cli(
        env["db"],
        tenant_id="t1",
        session=env["session"],
        task_frame_id="frame-1",
        agent_id="agent-1",
        arguments=arguments,
        active_skill=active_skill,
        active_step_id=active_step_id,
    )


def test_service_disabled_returns_precondition(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(
        service, "get_settings", lambda: Settings(lark_cli_enabled=False)
    )
    chat = ChatSession(id="sess-0", tenant_id="t1", user_id="u1")
    result = service.invoke_lark_cli(
        db,
        tenant_id="t1",
        session=chat,
        task_frame_id="frame-0",
        agent_id=None,
        arguments={"args": ["auth", "status"]},
    )
    assert result["success"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    assert result["error"]["subcode"] == "LARK_CLI_DISABLED"


def test_service_unconfigured_guides_conversational_setup(lark_env_unbound) -> None:
    """无 settings 凭据、无渠道绑定、HOME 未配置 → 引导对话内配置而不是硬失败。"""

    result = _invoke(lark_env_unbound, {"args": ["auth", "status"]})
    assert result["success"] is False
    assert result["error"]["subcode"] == "LARK_CLI_APP_NOT_CONFIGURED"
    message = result["error"]["message"]
    assert "--app-id" in message and "--new" in message


def test_service_conversational_config_works_without_binding(
    lark_env_unbound,
) -> None:
    """零配置路径：无 .env 凭据、无渠道绑定，对话内 config init 即可用。"""

    home = runner.user_home_dir("t1", "u1")
    assert not runner.configured_app_ids(home)

    configured = _invoke(
        lark_env_unbound,
        {
            "args": [
                "config", "init", "--app-id", "cli_chat_app",
                "--app-secret", "chat-secret",
            ]
        },
    )
    assert configured["success"] is True
    assert "cli_chat_app" in runner.configured_app_ids(home)

    # 配置后普通命令直接可用，全程未经过 settings 或渠道绑定。
    status = _invoke(lark_env_unbound, {"args": ["auth", "status"]})
    assert status["success"] is True
    # secret 经 stdin 注入，不得出现在任何返回结果里。
    assert "chat-secret" not in json.dumps(
        [configured, status], ensure_ascii=False
    )


def test_service_read_command_runs_and_configures_home(lark_env) -> None:
    result = _invoke(lark_env, {"args": ["auth", "status"]})
    # auth status 信封没有 ok 字段，exit 0 且无 error 必须判成功。
    assert result["success"] is True
    envelope = result["data"]["envelope"]
    assert envelope["identities"]["user"]["available"] is False
    assert "--json" in envelope["argv"]
    home = runner.user_home_dir("t1", "u1")
    assert "cli_test_app" in runner.configured_app_ids(home)


def test_service_injects_as_user_for_approval_commands(lark_env) -> None:
    result = _invoke(
        lark_env,
        {"args": ["approval", "approvals", "search", "--data", '{"keyword":"请假"}']},
    )
    assert result["success"] is True
    argv = result["data"]["envelope"]["argv"]
    assert argv[argv.index("--as") + 1] == "user"


def test_service_allows_help_and_auth_scopes(lark_env) -> None:
    helped = _invoke(lark_env, {"args": ["auth", "login", "--help"]})
    assert helped["success"] is True
    # --help 时不触发提交闸：即便是 gated_write 前缀也按只读处理。
    create_help = _invoke(
        lark_env, {"args": ["approval", "instances", "create", "--help"]}
    )
    assert create_help["success"] is True
    scopes = _invoke(lark_env, {"args": ["auth", "scopes", "--json"]})
    assert scopes["success"] is True


def test_policy_blocks_auth_qrcode_with_redirect_guidance() -> None:
    """真实案例：模型每次登录都先试 qrcode，白烧一轮。拒绝文案必须直接
    指向正确动作（发 verification_url 链接），而非笼统的"未登记"。"""

    with pytest.raises(policy.LarkCliPolicyError) as exc:
        policy.resolve(["auth", "qrcode", "--url", "https://x", "--output", "q.png"])
    assert "verification_url" in exc.value.message


def test_service_auth_scopes_failure_redirects_to_login(lark_env) -> None:
    """真实案例：auth scopes 因自身缺管理权限而失败，模型误读为"应用没有
    审批权限"并 finish(failed)。错误消息必须澄清失败归属并重定向到 auth login。"""

    result = _invoke(lark_env, {"args": ["auth", "scopes", "--trigger_error"]})
    assert result["success"] is False
    message = result["error"]["message"]
    assert "不代表任务失败" in message
    assert "auth login" in message


def test_service_dry_run_returns_digest(lark_env) -> None:
    data = '{"approval_code":"A","form":"[]"}'
    result = _invoke(
        lark_env,
        {"args": ["approval", "instances", "create", "--data", data, "--dry-run"]},
    )
    assert result["success"] is True
    digest = result["data"]["submission_digest"]
    assert digest.startswith("sha256:")
    assert "--as" in result["data"]["envelope"]["argv"]
    # 规范化提交体随预览结果返回并落库，供提交节点零参数重放。
    assert result["data"]["canonical_data"] == {"approval_code": "A", "form": []}


def test_service_dry_run_normalizes_array_form_to_wire_string(lark_env) -> None:
    data = '{"approval_code":"A","form":[{"id":"w1","value":"x"}]}'
    result = _invoke(
        lark_env,
        {"args": ["approval", "instances", "create", "--data", data, "--dry-run"]},
    )
    assert result["success"] is True
    argv = result["data"]["envelope"]["argv"]
    sent = json.loads(argv[argv.index("--data") + 1])
    assert isinstance(sent["form"], str)


def test_service_submit_accepts_cross_encoding_confirmation(lark_env) -> None:
    """预览用数组 form、提交用字符串 form：语义相同必须通过摘要闸（回归）。"""

    array_data = '{"approval_code":"A","form":[{"id":"w1","value":"x"}]}'
    string_data = json.dumps(
        {"approval_code": "A", "form": '[{"id": "w1", "value": "x"}]'},
        ensure_ascii=False,
    )
    digest = policy.submission_digest(json.loads(array_data))
    assert digest == policy.submission_digest(json.loads(string_data))
    lark_env["db"].add(
        HarnessInvocationRecord(
            tenant_id="t1",
            session_id="sess-1",
            task_id="frame-1",
            run_id="run-x",
            call_id="call-x",
            tool_name=service.TOOL_NAME,
            request_digest="rdx",
            status="completed",
            response_cache_json={"success": True, "data": {"submission_digest": digest}},
        )
    )
    lark_env["db"].commit()
    result = _invoke(
        lark_env,
        {
            "args": ["approval", "instances", "create", "--data", string_data],
            "confirmed_form_digest": digest,
        },
        active_skill=_submit_skill(),
        active_step_id="n_submit",
    )
    assert result["success"] is True
    argv = result["data"]["envelope"]["argv"]
    sent = json.loads(argv[argv.index("--data") + 1])
    assert isinstance(sent["form"], str) and sent["uuid"]


def test_service_submit_requires_confirmation_and_preview(lark_env) -> None:
    data = '{"approval_code":"A","form":"[]"}'
    submit_args = {"args": ["approval", "instances", "create", "--data", data]}
    skill = _submit_skill()

    missing = _invoke(lark_env, submit_args, active_skill=skill, active_step_id="n_submit")
    assert missing["error"]["subcode"] == "LARK_CLI_CONFIRMATION_REQUIRED"

    digest = policy.submission_digest(json.loads(data))
    mismatch = _invoke(
        lark_env,
        {**submit_args, "confirmed_form_digest": "sha256:wrong"},
        active_skill=skill,
        active_step_id="n_submit",
    )
    assert mismatch["error"]["subcode"] == "LARK_CLI_PREVIEW_DIGEST_MISMATCH"

    no_preview = _invoke(
        lark_env,
        {**submit_args, "confirmed_form_digest": digest},
        active_skill=skill,
        active_step_id="n_submit",
    )
    assert no_preview["error"]["subcode"] == "LARK_CLI_PREVIEW_NOT_FOUND"


def test_service_submit_blocked_outside_authorizing_sop_step(lark_env) -> None:
    """流程闸：即便摘要与预览记录都合法，非授权节点/会话闲聊帧不得提交。"""

    data = '{"approval_code":"A","form":"[]"}'
    digest = policy.submission_digest(json.loads(data))
    lark_env["db"].add(
        HarnessInvocationRecord(
            tenant_id="t1",
            session_id="sess-1",
            task_id="frame-1",
            run_id="run-0",
            call_id="call-0",
            tool_name=service.TOOL_NAME,
            request_digest="rd0",
            status="completed",
            response_cache_json={"success": True, "data": {"submission_digest": digest}},
        )
    )
    lark_env["db"].commit()
    submit_args = {
        "args": ["approval", "instances", "create", "--data", data],
        "confirmed_form_digest": digest,
    }

    conversation = _invoke(lark_env, submit_args)
    assert conversation["error"]["subcode"] == "LARK_CLI_SUBMIT_REQUIRES_SOP"
    # 拒绝消息必须重定向模型到正确动作（真实案例：旧文案导致模型误判任务失败）。
    assert "next_step_id" in conversation["error"]["message"]
    assert "不是任务失败" in conversation["error"]["message"]

    other_step_skill = _submit_skill()
    other_step_skill.content_json = {
        "start_node_id": "n_chat",
        "nodes": [{"node_id": "n_chat", "name": "闲聊", "capability_refs": {}}],
        "edges": [],
    }
    unauthorized = _invoke(
        lark_env, submit_args, active_skill=other_step_skill, active_step_id="n_chat"
    )
    assert unauthorized["error"]["subcode"] == "LARK_CLI_SUBMIT_REQUIRES_SOP"

    # dry-run 与读命令不受流程闸限制。
    preview = _invoke(
        lark_env,
        {"args": ["approval", "instances", "create", "--data", data, "--dry-run"]},
    )
    assert preview["success"] is True


def test_service_submit_happy_path_injects_yes_and_uuid(lark_env) -> None:
    data = '{"approval_code":"A","form":"[]"}'
    digest = policy.submission_digest(json.loads(data))
    lark_env["db"].add(
        HarnessInvocationRecord(
            tenant_id="t1",
            session_id="sess-1",
            task_id="frame-1",
            run_id="run-1",
            call_id="call-1",
            tool_name=service.TOOL_NAME,
            request_digest="rd",
            status="completed",
            response_cache_json={
                "success": True,
                "data": {"submission_digest": digest},
            },
        )
    )
    lark_env["db"].commit()

    result = _invoke(
        lark_env,
        {
            "args": ["approval", "instances", "create", "--data", data],
            "confirmed_form_digest": digest,
        },
        active_skill=_submit_skill(),
        active_step_id="n_submit",
    )
    assert result["success"] is True
    argv = result["data"]["envelope"]["argv"]
    assert argv[-1] == "--yes"
    assert "--as" in argv and argv[argv.index("--as") + 1] == "user"
    sent_data = json.loads(argv[argv.index("--data") + 1])
    assert sent_data["uuid"]
    assert sent_data["approval_code"] == "A"
    # 同一帧同一内容 → uuid 稳定（服务端幂等的前提）。
    assert sent_data["uuid"] == service._submission_uuid("frame-1", digest)


def _add_preview_record(
    env,
    *,
    digest: str,
    canonical: dict | None,
    task_id: str = "frame-0",
    run_id: str = "run-p",
    started_at: datetime | None = None,
) -> None:
    data: dict = {"submission_digest": digest}
    if canonical is not None:
        data["canonical_data"] = canonical
    env["db"].add(
        HarnessInvocationRecord(
            tenant_id="t1",
            session_id="sess-1",
            task_id=task_id,
            run_id=run_id,
            call_id=f"call-{run_id}",
            tool_name=service.TOOL_NAME,
            request_digest=f"rd-{run_id}",
            status="completed",
            response_cache_json={"success": True, "data": data},
            started_at=started_at or datetime.now(UTC),
        )
    )
    env["db"].commit()


def test_service_submit_without_data_replays_latest_preview(lark_env) -> None:
    """零参数提交：受信层重放会话内最近一次预览的规范化内容（跨帧核心修复）。

    真实案例：流程推进到提交节点的新帧后，模型丢失了预览帧的
    表单上下文，重组时自造控件 id（widgetLeaveGroupType）被服务端打回。
    """

    canonical = {"approval_code": "A", "form": [{"id": "w1", "value": "x"}]}
    digest = policy.submission_digest(dict(canonical))
    _add_preview_record(lark_env, digest=digest, canonical=canonical)

    result = _invoke(
        lark_env,
        {"args": ["approval", "instances", "create"]},
        active_skill=_submit_skill(),
        active_step_id="n_submit",
    )
    assert result["success"] is True
    argv = result["data"]["envelope"]["argv"]
    assert argv[-1] == "--yes"
    sent = json.loads(argv[argv.index("--data") + 1])
    assert sent["approval_code"] == "A"
    # 线上格式：form 序列化为 JSON 字符串，内容与预览记录逐字段一致。
    assert json.loads(sent["form"]) == canonical["form"]
    assert sent["uuid"] == service._submission_uuid("frame-1", digest)


def test_service_submit_without_data_requires_replayable_preview(lark_env) -> None:
    no_record = _invoke(
        lark_env,
        {"args": ["approval", "instances", "create"]},
        active_skill=_submit_skill(),
        active_step_id="n_submit",
    )
    assert no_record["error"]["subcode"] == "LARK_CLI_PREVIEW_NOT_FOUND"

    # 最近一条预览缺少 canonical_data（旧记录）时同样拒绝，绝不回退到更早
    # 的内容——重放陈旧表单比报错更危险。
    canonical = {"approval_code": "A", "form": []}
    old_digest = policy.submission_digest(dict(canonical))
    base = datetime.now(UTC)
    _add_preview_record(
        lark_env,
        digest=old_digest,
        canonical=canonical,
        run_id="run-old",
        started_at=base - timedelta(minutes=5),
    )
    _add_preview_record(
        lark_env,
        digest="sha256:newer-without-canonical",
        canonical=None,
        run_id="run-new",
        started_at=base,
    )
    stale = _invoke(
        lark_env,
        {"args": ["approval", "instances", "create"]},
        active_skill=_submit_skill(),
        active_step_id="n_submit",
    )
    assert stale["error"]["subcode"] == "LARK_CLI_PREVIEW_NOT_FOUND"


def test_service_submit_without_data_still_requires_sop_step(lark_env) -> None:
    """零参数重放不弱化结构闸：非授权节点照样拒绝。"""

    canonical = {"approval_code": "A", "form": []}
    _add_preview_record(
        lark_env,
        digest=policy.submission_digest(dict(canonical)),
        canonical=canonical,
    )
    blocked = _invoke(lark_env, {"args": ["approval", "instances", "create"]})
    assert blocked["error"]["subcode"] == "LARK_CLI_SUBMIT_REQUIRES_SOP"


def test_service_submit_digest_path_accepts_cross_frame_preview(lark_env) -> None:
    """显式 --data 路径的预览记录查找放宽到会话级（预览与提交常跨帧）。"""

    data = '{"approval_code":"A","form":"[]"}'
    digest = policy.submission_digest(json.loads(data))
    _add_preview_record(
        lark_env, digest=digest, canonical=None, task_id="frame-other"
    )
    result = _invoke(
        lark_env,
        {
            "args": ["approval", "instances", "create", "--data", data],
            "confirmed_form_digest": digest,
        },
        active_skill=_submit_skill(),
        active_step_id="n_submit",
    )
    assert result["success"] is True


def test_logical_write_signature_covers_no_data_submit() -> None:
    """零参数提交也要有防重放签名（帧内重试命中重放缓存而非重复提交）。"""

    signature = policy.logical_write_signature(
        {"args": ["approval", "instances", "create"]}
    )
    assert signature is not None
    # 与显式 --data 的提交签名不同：空内容摘要 vs 实际内容摘要。
    explicit = policy.logical_write_signature(
        {
            "args": [
                "approval",
                "instances",
                "create",
                "--data",
                '{"approval_code":"A","form":"[]"}',
            ]
        }
    )
    assert explicit is not None and explicit != signature


_LEAVE_DEFINITION = [
    {
        "id": "widgetLeaveGroupV2",
        "type": "leaveGroupV2",
        "value": [
            {
                "id": "widgetLeaveGroupType",
                "type": "radioV2",
                "option": [
                    {"value": "7678264870298471375", "text": "事假"},
                    {"value": "7678264870520835028", "text": "病假"},
                ],
            },
            {"id": "widgetLeaveGroupStartTime", "type": "date"},
            {"id": "widgetLeaveGroupReason", "type": "textarea"},
        ],
    },
    {"id": "widgetRemark", "type": "textarea"},
]


def test_validate_form_catches_real_failure_modes() -> None:
    """真实案例合集：平铺复合子控件 / 自造 id / 选项用文字 / type 不符。"""

    flattened = [{"id": "widgetLeaveGroupType", "type": "radioV2", "value": "x"}]
    errors = policy.validate_form_against_definition(flattened, _LEAVE_DEFINITION)
    assert any("嵌套" in e and "widgetLeaveGroupV2" in e for e in errors)

    unknown = [{"id": "widgetMadeUp", "type": "input", "value": "x"}]
    errors = policy.validate_form_against_definition(unknown, _LEAVE_DEFINITION)
    assert any("不存在" in e for e in errors)

    text_not_key = [
        {
            "id": "widgetLeaveGroupV2",
            "type": "leaveGroupV2",
            "value": [
                {"id": "widgetLeaveGroupType", "type": "radioV2", "value": "事假"}
            ],
        }
    ]
    errors = policy.validate_form_against_definition(text_not_key, _LEAVE_DEFINITION)
    assert any("7678264870298471375" in e for e in errors)

    wrong_type = [{"id": "widgetRemark", "type": "input", "value": "x"}]
    errors = policy.validate_form_against_definition(wrong_type, _LEAVE_DEFINITION)
    assert any("type" in e for e in errors)

    not_array = [{"id": "widgetLeaveGroupV2", "type": "leaveGroupV2", "value": "x"}]
    errors = policy.validate_form_against_definition(not_array, _LEAVE_DEFINITION)
    assert any("子控件数组" in e for e in errors)


def test_validate_form_accepts_correct_nesting_and_missing_optional() -> None:
    good = [
        {
            "id": "widgetLeaveGroupV2",
            "type": "leaveGroupV2",
            "value": [
                {
                    "id": "widgetLeaveGroupType",
                    "type": "radioV2",
                    "value": "7678264870298471375",
                },
                {
                    "id": "widgetLeaveGroupStartTime",
                    "type": "date",
                    "value": "2026-09-01T09:00:00+08:00",
                },
                {"id": "widgetLeaveGroupReason", "type": "textarea", "value": "测试"},
            ],
        }
        # widgetRemark 缺省：宽松处交给服务端裁决，不报错。
    ]
    assert policy.validate_form_against_definition(good, _LEAVE_DEFINITION) == []


def test_service_dry_run_blocks_form_mismatch_before_preview(lark_env) -> None:
    """CLI 的 --dry-run 不打服务端（真实案例：编造控件通过预览、用户确认后
    才在真实提交时暴雷）——受信层用定义原文在预览前拦截。"""

    flattened = json.dumps(
        {
            "approval_code": "LEAVE_DEF",
            "form": [{"id": "widgetLeaveGroupType", "type": "radioV2", "value": "x"}],
        },
        ensure_ascii=False,
    )
    blocked = _invoke(
        lark_env,
        {"args": ["approval", "instances", "create", "--data", flattened, "--dry-run"]},
    )
    assert blocked["success"] is False
    assert blocked["error"]["subcode"] == "LARK_CLI_FORM_MISMATCH"
    assert "嵌套" in blocked["error"]["message"]

    corrected = json.dumps(
        {
            "approval_code": "LEAVE_DEF",
            "form": [
                {
                    "id": "widgetLeaveGroupV2",
                    "type": "leaveGroupV2",
                    "value": [
                        {
                            "id": "widgetLeaveGroupType",
                            "type": "radioV2",
                            "value": "7678264870298471375",
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )
    ok = _invoke(
        lark_env,
        {"args": ["approval", "instances", "create", "--data", corrected, "--dry-run"]},
    )
    assert ok["success"] is True
    assert ok["data"]["submission_digest"].startswith("sha256:")


def test_service_dry_run_skips_validation_when_definition_unavailable(lark_env) -> None:
    """定义获取失败/无定义数据时不阻塞预览（校验是增强不是门槛）。"""

    data = '{"approval_code":"A","form":[{"id":"anything","value":"x"}]}'
    result = _invoke(
        lark_env,
        {"args": ["approval", "instances", "create", "--data", data, "--dry-run"]},
    )
    assert result["success"] is True


def test_service_translates_cli_error_envelope(lark_env) -> None:
    result = _invoke(
        lark_env,
        {"args": ["approval", "approvals", "search", "--data", '{"keyword":"trigger_error"}']},
    )
    assert result["success"] is False
    assert result["error"]["code"] == "LARK_CLI_PERMISSION_DENIED"
    assert "auth login" in result["error"]["message"]


def test_service_typed_flag_read_runs_directly(lark_env) -> None:
    """官方 typed 参数（--approval-code）零试错直达，且自动注入 --as user。"""

    result = _invoke(
        lark_env,
        {"args": ["approval", "approvals", "get", "--approval-code", "CODE-1"]},
    )
    assert result["success"] is True
    argv = result["data"]["envelope"]["argv"]
    assert "--approval-code" in argv and "CODE-1" in argv
    assert argv[argv.index("--as") + 1] == "user"


def test_service_dynamic_read_allowed_by_cli_risk(lark_env) -> None:
    """未登记命令：CLI 自标 Risk: read 即放行，并按 help 注入 --as user。"""

    result = _invoke(lark_env, {"args": ["im", "messages", "list", "--page-size", "5"]})
    assert result["success"] is True
    argv = result["data"]["envelope"]["argv"]
    assert argv[:3] == ["im", "messages", "list"]
    assert argv[argv.index("--as") + 1] == "user"


def test_service_dynamic_write_denied_by_cli_risk(lark_env) -> None:
    """未登记的写命令（审批处置等）即便探测也不放行，需显式登记。"""

    result = _invoke(
        lark_env, {"args": ["approval", "tasks", "approve", "--data", "{}"]}
    )
    assert result["success"] is False
    assert result["error"]["subcode"] == "LARK_CLI_COMMAND_BLOCKED"
    assert "策略表" in result["error"]["message"]
    # 真实案例：模型曾建议用户"在本地终端运行 lark-cli auth logout"——登录态
    # 在系统托管的隔离 HOME 里，终端操作无效。拒绝文案必须堵住这类误导。
    assert "不要建议用户在本地终端" in result["error"]["message"]


def test_service_auth_logout_allowed(lark_env) -> None:
    """换账号闭环：logout 在免确认写名单内，直接执行并自动补 --json。"""

    result = _invoke(lark_env, {"args": ["auth", "logout"]})
    assert result["success"] is True
    argv = result["data"]["envelope"]["argv"]
    assert argv[:2] == ["auth", "logout"] and "--json" in argv


def test_policy_safe_write_allowlist_is_local_state_only() -> None:
    """守护测试：免确认写名单只允许"纯本地态操作"，防止以后误把有外部
    副作用的命令（如 im messages send）加成 write 规则绕过确认。"""

    local_state_only = {("auth", "login"), ("auth", "logout"), ("config", "init")}
    for rule in policy._RULES:
        if rule.action == "write":
            assert rule.prefix in local_state_only, rule.prefix
        elif rule.action == "gated_write":
            assert rule.prefix == ("approval", "instances", "create"), rule.prefix


def test_policy_config_init_rules() -> None:
    resolved = policy.resolve(
        ["config", "init", "--app-id", "cli_x", "--app-secret", "s3cret"]
    )
    assert resolved.rule.action == "write" and not resolved.is_help
    assert policy.resolve(["config", "init", "--new"]).rule.prefix == ("config", "init")
    # config 域其余子命令与危险 flag 保持封禁；--help 放行。
    with pytest.raises(policy.LarkCliPolicyError):
        policy.resolve(["config", "bind"])
    with pytest.raises(policy.LarkCliPolicyError):
        policy.resolve(["config", "init", "--new", "--force-init"])
    assert policy.resolve(["config", "init", "--help"]).is_help
    # 应用配置是可重试的写操作，不纳入防重放签名。
    assert (
        policy.logical_write_signature(
            {"args": ["config", "init", "--app-id", "a", "--app-secret", "b"]}
        )
        is None
    )


def test_service_conversational_config_init_then_commands_work(lark_env_unbound) -> None:
    """对话内提供 app_id/secret 完成配置后，后续命令无需绑定/settings 即可运行。"""

    result = _invoke(
        lark_env_unbound,
        {"args": ["config", "init", "--app-id", "cli_chat_app", "--app-secret", "chat-secret"]},
    )
    assert result["success"] is True
    assert result["data"]["configured_app_ids"] == ["cli_chat_app"]
    # secret 不出现在结果里（进入对话是用户自己的选择，但执行面必须脱敏）。
    assert "chat-secret" not in json.dumps(result, ensure_ascii=False)
    followup = _invoke(lark_env_unbound, {"args": ["auth", "status"]})
    assert followup["success"] is True


def test_service_config_init_requires_credentials_or_new(lark_env_unbound) -> None:
    result = _invoke(lark_env_unbound, {"args": ["config", "init", "--app-id", "cli_x"]})
    assert result["error"]["subcode"] == "LARK_CLI_CONFIG_ARGS_REQUIRED"


def test_service_config_init_new_pending_then_configured(lark_env_unbound) -> None:
    """--new 后台流程：先拿到 verification_url，用户完成后查询到 configured。"""

    first = _invoke(lark_env_unbound, {"args": ["config", "init", "--new"]})
    assert first["success"] is True
    assert first["data"]["status"] == "pending_user"
    assert "open.feishu.cn" in str(first["data"]["verification_url"])
    # 模拟用户在浏览器完成创建：CLI 会把新应用写进 HOME 配置。
    home = runner.user_home_dir("t1", "u1")
    config_dir = home / ".lark-cli"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"apps": [{"appId": "cli_new_app", "brand": "feishu"}]}),
        encoding="utf-8",
    )
    second = _invoke(lark_env_unbound, {"args": ["config", "init", "--new"]})
    assert second["success"] is True
    assert second["data"]["status"] == "configured"
    assert second["data"]["app_ids"] == ["cli_new_app"]
    followup = _invoke(lark_env_unbound, {"args": ["auth", "status"]})
    assert followup["success"] is True


def test_audit_arguments_redact_secret_flag_values() -> None:
    """对话内提供的 --app-secret 值不得明文落进 harness_invocations 审计记录。"""

    from app.core.harness_capability_invoker import _audit_arguments

    audited = _audit_arguments(
        {"args": ["config", "init", "--app-id", "cli_x", "--app-secret", "s3cret"]}
    )
    assert audited["args"] == [
        "config", "init", "--app-id", "cli_x", "--app-secret", "<redacted>"
    ]
    # 普通 argv 不受影响。
    assert _audit_arguments({"args": ["auth", "status"]})["args"] == ["auth", "status"]


def test_background_failure_reports_output_tail(tmp_path: Path) -> None:
    from app.lark_cli import background

    binary = tmp_path / "failing-cli"
    binary.write_text(
        "#!/usr/bin/env python3\nimport sys\nprint('boom: cannot create app')\nsys.exit(2)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    status = background.config_init_new_status(binary, home)
    assert status["status"] == "failed"
    assert "boom" in str(status["detail"])
    assert str(home) not in background._REGISTRY


def test_service_secret_never_reaches_argv_or_output(lark_env) -> None:
    result = _invoke(lark_env, {"args": ["auth", "status"]})
    blob = json.dumps(result, ensure_ascii=False)
    assert "app-secret-value" not in blob


# ---------------------------------------------------------------------------
# manifest descriptor
# ---------------------------------------------------------------------------


def test_lark_cli_reserved_name() -> None:
    assert "lark_cli" in RESERVED_HARNESS_CAPABILITY_NAMES


def test_sample_sop_card_is_valid_and_authorizes_submission() -> None:
    """docs/lark-cli-approval-sop.json 必须始终能通过 SkillCard 校验。"""

    from app.core.task_request_compiler import current_step_capability_refs
    from app.skills.skill_schema import SkillCard

    card_path = (
        Path(__file__).resolve().parents[2] / "docs" / "lark-cli-approval-sop.json"
    )
    card = SkillCard.model_validate(json.loads(card_path.read_text(encoding="utf-8")))
    # 鉴权必须先于任何飞书调用：收集节点本身要检索审批定义（需登录态），
    # 起点若排在它之后，未登录用户必然先撞一次 TOKEN_MISSING（真实案例）。
    assert card.start_node_id == "n_auth_check"
    edges = {
        (edge.source_node_id, edge.next_node_id) for edge in card.edges
    }
    assert ("n_auth_check", "n_collect") in edges
    assert ("n_auth_complete", "n_collect") in edges
    assert ("n_collect", "n_preview") in edges

    skill = Skill(
        tenant_id="t1",
        skill_id=card.skill_id,
        name=card.name,
        content_json=card.model_dump(mode="json"),
    )
    refs = current_step_capability_refs(skill, "n_submit")
    assert "lark_cli" in refs["tool_ids"]
    assert service._step_authorizes_submission(skill, "n_submit") is True
    # 预览节点不得持有提交授权：用户确认前模型在该节点无法真实提交。
    assert service._step_authorizes_submission(skill, "n_preview") is False
    # 确认节点（n_preview）声明了必须等待的用户信息 → awaiting_user 流程闸成立。
    preview_node = next(
        node for node in card.nodes if node.node_id == "n_preview"
    )
    assert preview_node.expected_user_info


def test_gallery_seed_fixture_matches_docs_card() -> None:
    """扩展种子里的审批 SOP 必须与 docs 样例卡逐字节一致，防止双份漂移。"""

    root = Path(__file__).resolve().parents[2]
    card = json.loads(
        (root / "docs" / "lark-cli-approval-sop.json").read_text(encoding="utf-8")
    )
    fixture = json.loads(
        (
            root
            / "backend"
            / "app"
            / "db"
            / "seed_fixtures"
            / "staffdeck_expanded_gallery_seed.json"
        ).read_text(encoding="utf-8")
    )
    skill_rows = [
        row
        for row in fixture["skills"]
        if row.get("skill_id") == card["skill_id"]
    ]
    assert len(skill_rows) == 1
    assert skill_rows[0]["content_json"] == card
    assert skill_rows[0]["status"] == "published"
    version_rows = [
        row
        for row in fixture["skill_versions"]
        if row.get("skill_id") == card["skill_id"]
    ]
    assert len(version_rows) == 1 and version_rows[0]["content_json"] == card
    bindings = [
        row
        for row in fixture["agent_resource_bindings"]
        if row.get("resource_id") == skill_rows[0]["id"]
    ]
    assert len(bindings) == 1
    assert bindings[0]["resource_type"] == "skill"
    assert bindings[0]["status"] == "active"


def test_manifest_descriptor_gating(monkeypatch, db: Session) -> None:
    import app.core.capability_manifest as manifest_module

    monkeypatch.setattr(
        "app.config.get_settings", lambda: Settings(lark_cli_enabled=False)
    )
    assert manifest_module._lark_cli_descriptor(db, "t1", None) is None

    monkeypatch.setattr(
        "app.config.get_settings", lambda: Settings(lark_cli_enabled=True)
    )
    # 凭据可在对话内建立（config init），因此开启即视为可用，无需预检凭据。
    descriptor = _lark_cli_descriptor(db, "t1", None)
    assert descriptor is not None and descriptor.available is True
    assert descriptor.name == "lark_cli" and descriptor.kind == "internal"
    assert "config init" in descriptor.description
