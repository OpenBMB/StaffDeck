"""``lark_cli`` 内置能力的调用入口（由 HarnessCapabilityInvoker 转发）。

职责：身份与凭据解析 → 策略裁决 → 确认闸（真实提交必须命中本会话已
落库的 dry-run 预览；推荐零参数提交，由受信层重放预览内容）→ 受信
argv 重组（注入 --as user / uuid / --yes）→ 执行与错误翻译。

约定：所有"尚未产生外部副作用"的失败一律用主错误码
``INVALID_ARGUMENTS``（子码放 ``error.subcode``），这样 invoker 的
``_failure_was_not_sent`` 会释放防重放声明，允许修正后重试；执行后
的失败保留具体错误码，声明被保守地保留。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any

from cryptography.fernet import InvalidToken
from sqlmodel import select

from app.channels.crypto import decrypt_channel_secret
from app.config import get_settings
from app.db.models import ChannelBinding, ChatSession, HarnessInvocationRecord
from app.lark_cli import background, policy
from app.lark_cli.provision import LarkCliProvisionError, ensure_lark_cli
from app.lark_cli.runner import (
    LarkCliRunError,
    configured_app_ids,
    ensure_configured,
    run_lark_cli,
    user_home_dir,
)

TOOL_NAME = "lark_cli"

LARK_CLI_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 32,
            "description": (
                "lark-cli 的 argv（不含二进制名），例如 "
                '["approval","approvals","search","--data","{\\"keyword\\":\\"报销\\"}"]。'
            ),
        },
        "confirmed_form_digest": {
            "type": "string",
            "description": (
                "仅当真实提交（approval instances create 且无 --dry-run）显式携带 "
                "--data 时需要：原样回传 dry-run 预览结果里的 submission_digest。"
                "推荐做法是提交时不带 --data 也不带本字段，系统会自动重放本会话"
                "最近一次 dry-run 预览（即用户确认过）的内容。"
            ),
        },
    },
    "required": ["args"],
    "additionalProperties": False,
}

LARK_CLI_DESCRIPTION = (
    "运行飞书官方 lark-cli（读宽写严：只读命令支持官方原生参数直接调用，"
    "如 approval approvals get --approval-code <code>；未登记的只读命令也"
    "会按 CLI 自身的 Risk 分级自动放行；写命令仅限白名单：auth 登录/登出、"
    "approval 实例 dry-run 预览与创建、config init 应用配置）。用户要换"
    "飞书账号时：auth logout 清除当前登录态，再重新走设备码登录流程。若报应用未配置，可在对话中完成：用户提供"
    "现有应用凭据则 config init --app-id <id> --app-secret <secret>；或 "
    "config init --new 现场创建新应用（把返回的 verification_url 发给用户，"
    "完成后重复调用查询进度）。登录走设备码分回合流程：auth login "
    "--scope ... --no-wait --json 拿到 verification_url 先发给用户，用户"
    "确认授权后再用 --device-code 收尾。真实提交前必须先 --dry-run 预览、"
    "把表单逐字段展示给用户并获得明确同意；确认后在提交节点直接调用 "
    "approval instances create（不带 --data），系统会自动重放已确认的预览"
    "内容并注入 --yes 与幂等 uuid，禁止自带。"
)


def invoke_lark_cli(
    db: Any,
    *,
    tenant_id: str,
    session: ChatSession,
    task_frame_id: str,
    agent_id: str | None,
    arguments: dict[str, Any],
    active_skill: Any | None = None,
    active_step_id: str | None = None,
) -> dict[str, Any]:
    if not get_settings().lark_cli_enabled:
        return _precondition_failure(
            "LARK_CLI_DISABLED", "lark-cli 集成未启用（settings.lark_cli_enabled）。"
        )
    raw_args = arguments.get("args")
    if not isinstance(raw_args, list) or not raw_args:
        return _precondition_failure("LARK_CLI_ARGS_REQUIRED", "args 必须是非空字符串数组。")
    try:
        resolved = policy.resolve([str(item) for item in raw_args])
    except policy.LarkCliPolicyError as exc:
        return _precondition_failure(exc.code, exc.message)

    user_key = str(session.user_id or "").strip()
    if not user_key:
        return _precondition_failure(
            "LARK_CLI_USER_UNRESOLVED",
            "当前会话没有可用的 StaffDeck 用户身份，无法定位该用户的飞书登录态。",
        )
    credential_error, app_id, app_secret = _resolve_app_credentials(db, tenant_id, agent_id)
    if credential_error is not None:
        return credential_error

    argv = list(resolved.argv)
    # 审批域命令只允许 user 身份（policy 已锁死取值），由受信代码统一注入，
    # 不依赖模型记得传 --as user（CLI 侧缺省会解析成 bot 而报错）。
    if argv[0] == "approval" and not resolved.is_help:
        argv = _ensure_flag_pair(argv, "--as", "user")
    stashed_digest: str | None = None
    stashed_body: dict[str, Any] | None = None
    if resolved.is_help:
        pass  # --help 纯只读，跳过所有提交闸。
    elif resolved.rule.action == "gated_write" and not resolved.is_dry_run:
        if not _step_authorizes_submission(active_skill, active_step_id):
            # 真实案例：模型收到旧版拒绝文案后误判"任务失败"，在提交节点
            # 空手 finish(failed)。消息必须重定向到正确动作，而不是只说不行。
            return _precondition_failure(
                "LARK_CLI_SUBMIT_REQUIRES_SOP",
                "当前步骤不允许执行真实提交（只有 capability_refs.tool_ids 显式"
                "包含 lark_cli 的 SOP 提交节点可以）。这不是任务失败：请立即"
                "结束当前步骤并给出 next_step_id 进入提交节点，进入提交节点后"
                "第一时间直接调用 approval instances create（不带 --data），"
                "系统会自动提交用户确认过的预览内容。登录、查询与 --dry-run "
                "预览不受此限制。",
            )
        try:
            gate_error, argv = _apply_submission_gate(
                db,
                session_id=session.id,
                task_frame_id=task_frame_id,
                arguments=arguments,
                resolved=resolved,
            )
        except policy.LarkCliPolicyError as exc:
            return _precondition_failure(exc.code, exc.message)
        if gate_error is not None:
            return gate_error
    elif resolved.rule.action == "gated_write" and resolved.is_dry_run:
        try:
            stashed_digest = policy.submission_digest(resolved.data_json)
            # 规范化提交体随预览结果落库（response_cache_json），供提交节点
            # 跨帧零参数重放——模型在新帧里重组表单曾多次自造控件 id。
            body = policy.canonical_submission_body(resolved.data_json)
            stashed_body = body if isinstance(body, dict) else None
            if resolved.data_json is not None:
                # 预览与提交走同一线上格式（form 序列化为 JSON 字符串），
                # 保证 dry-run 展示的就是真正会发出去的请求体。
                argv = _replace_flag_value(
                    argv,
                    "--data",
                    json.dumps(
                        policy.wire_submission_data(resolved.data_json),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
        except policy.LarkCliPolicyError as exc:
            return _precondition_failure(exc.code, exc.message)
        argv = _ensure_flag_pair(argv, "--as", "user")

    if argv[0] == "auth" and "--json" not in argv:
        argv.append("--json")

    try:
        binary = ensure_lark_cli()
    except LarkCliProvisionError as exc:
        return _precondition_failure("LARK_CLI_NOT_INSTALLED", str(exc))
    home = user_home_dir(tenant_id, user_key)
    if resolved.rule.prefix == ("config", "init") and not resolved.is_help:
        return _handle_config_init(binary, home, argv)
    if resolved.needs_risk_probe and not resolved.is_help:
        risk, help_text = _probe_command_risk(binary, home, resolved.rule.prefix)
        if risk != "read":
            detail = (
                f"CLI 将其标记为 {risk}（写操作需在策略表显式登记后才可用）"
                if risk
                else "无法确认其为只读命令"
            )
            return _precondition_failure(
                "LARK_CLI_COMMAND_BLOCKED",
                f"命令 {' '.join(resolved.rule.prefix)} 未在策略表登记，且{detail}；"
                "只读（Risk: read）命令可直接使用。请如实告知用户此操作暂不"
                "支持（可由管理员评估后开放）；不要建议用户在本地终端自行执行"
                "——飞书登录态保存在系统托管的隔离环境中，用户终端里的 "
                "lark-cli 既看不到它也操作不了它。",
            )
        if "--as" in help_text and "--as" not in argv:
            argv = [*argv, "--as", "user"]
    try:
        if app_id and app_secret:
            ensure_configured(binary, home, app_id=app_id, app_secret=app_secret)
        elif not configured_app_ids(home):
            # 三级凭据（settings / 渠道绑定 / 对话内配置）全部缺失。
            return _precondition_failure(
                "LARK_CLI_APP_NOT_CONFIGURED",
                "尚无飞书应用凭据，可直接在对话中完成配置：① 用户已有应用："
                "请用户提供 app_id 与 app_secret（开放平台应用详情页可查），"
                "然后调用 config init --app-id <id> --app-secret <secret>；"
                "② 现场创建：调用 config init --new，把返回的 verification_url "
                "发给用户在浏览器完成创建，之后重复同命令查询进度。",
            )
        if resolved.is_dry_run and isinstance(stashed_body, dict):
            # CLI 的 --dry-run 不打服务端、零校验：编造的控件结构会一路
            # 通过预览、被用户确认，直到真实提交才暴雷。受信层拿定义原文
            # 在预览前做结构校验，把这类错误拦在用户确认之前。
            mismatch = _validate_form_structure(binary, home, stashed_body)
            if mismatch is not None:
                return mismatch
        result = run_lark_cli(
            binary,
            home,
            argv,
            timeout_seconds=resolved.rule.timeout_seconds,
            redact=(app_secret,),
        )
    except LarkCliRunError as exc:
        return {
            "success": False,
            "error": {"code": exc.code, "message": exc.message, "retryable": True},
        }
    finally:
        app_secret = ""

    return _translate(result, resolved, stashed_digest, stashed_body)


def _resolve_app_credentials(
    db: Any, tenant_id: str, agent_id: str | None
) -> tuple[dict[str, Any] | None, str, str]:
    """复用已激活的飞书渠道绑定里的应用凭据（若有）。

    返回 (错误或 None, app_id, app_secret 明文)。没有绑定时返回空凭据而非
    错误——常态是用户 HOME 已通过对话内 ``config init`` 配置过，是否可用由
    调用方结合 HOME 状态判定。用户级身份始终来自 ``auth login`` 设备码
    流程；这里只解决"应用载体"从哪来。
    """

    binding = _feishu_binding(db, tenant_id, agent_id)
    if binding is None or not binding.credentials_enc:
        return None, "", ""
    app_id = str((binding.config_json or {}).get("app_id") or "").strip()
    if not app_id:
        return None, "", ""
    try:
        app_secret = decrypt_channel_secret(binding.credentials_enc)
    except (InvalidToken, ValueError, TypeError):
        return (
            _precondition_failure(
                "LARK_CLI_CREDENTIALS_UNREADABLE",
                "飞书应用凭据解密失败，请检查 CHANNEL_SECRET/APP_SECRET 配置。",
            ),
            "",
            "",
        )
    return None, app_id, app_secret


_RISK_CACHE: dict[tuple[str, tuple[str, ...]], tuple[str | None, str]] = {}
_RISK_CACHE_LOCK = threading.Lock()
_RISK_PATTERN = re.compile(r"(?im)^\s*Risk:\s*(read|write|high-risk-write)\b")


def _probe_command_risk(
    binary: Any, home: Any, prefix: tuple[str, ...]
) -> tuple[str | None, str]:
    """未登记命令的动态裁决：跑一次 ``--help`` 读 CLI 自带的 Risk 分级。

    返回 (risk 或 None, help 文本)。结果按二进制路径+前缀缓存；解析不到
    Risk 行时返回 None（调用方按拒绝处理，保守默认）。
    """

    key = (str(binary), tuple(prefix))
    with _RISK_CACHE_LOCK:
        if key in _RISK_CACHE:
            return _RISK_CACHE[key]
    try:
        result = run_lark_cli(
            binary, home, [*prefix, "--help"], timeout_seconds=20.0
        )
    except LarkCliRunError:
        return None, ""
    text = f"{result.stdout}\n{result.stderr}"
    match = _RISK_PATTERN.search(text)
    risk = match.group(1).lower() if match else None
    with _RISK_CACHE_LOCK:
        _RISK_CACHE[key] = (risk, text)
    return risk, text


_DEFINITION_CACHE: dict[tuple[str, str], tuple[float, list[Any]]] = {}
_DEFINITION_CACHE_LOCK = threading.Lock()
_DEFINITION_TTL_SECONDS = 600.0


def _validate_form_structure(
    binary: Any, home: Any, body: dict[str, Any]
) -> dict[str, Any] | None:
    """dry-run 前的定义结构校验；返回错误结果或 None（通过/无法校验）。

    定义获取失败（网络等）时静默跳过，不阻塞预览——校验是增强而非门槛，
    最终裁决权在服务端。
    """

    approval_code = str(body.get("approval_code") or "").strip()
    form = body.get("form")
    if not approval_code or not isinstance(form, list):
        return None
    widgets = _approval_definition_widgets(binary, home, approval_code)
    if widgets is None:
        return None
    problems = policy.validate_form_against_definition(form, widgets)
    if not problems:
        return None
    return _precondition_failure(
        "LARK_CLI_FORM_MISMATCH",
        "表单与审批定义不一致，已在预览前拦截（按下述修正后重新 --dry-run）：\n- "
        + "\n- ".join(problems),
    )


def _approval_definition_widgets(
    binary: Any, home: Any, approval_code: str
) -> list[Any] | None:
    """获取并缓存审批定义的控件树（受信只读调用）；失败返回 None。"""

    key = (str(home), approval_code)
    now = time.monotonic()
    with _DEFINITION_CACHE_LOCK:
        cached = _DEFINITION_CACHE.get(key)
        if cached is not None and now - cached[0] < _DEFINITION_TTL_SECONDS:
            return cached[1]
    try:
        result = run_lark_cli(
            binary,
            home,
            [
                "approval",
                "approvals",
                "get",
                "--approval-code",
                approval_code,
                "--as",
                "user",
                "--json",
            ],
            timeout_seconds=30.0,
        )
    except LarkCliRunError:
        return None
    envelope = result.envelope if isinstance(result.envelope, dict) else {}
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else envelope
    form = data.get("form") if isinstance(data, dict) else None
    if isinstance(form, str):
        try:
            widgets = json.loads(form)
        except json.JSONDecodeError:
            return None
    else:
        widgets = form
    if not isinstance(widgets, list) or not widgets:
        return None
    with _DEFINITION_CACHE_LOCK:
        _DEFINITION_CACHE[key] = (now, widgets)
    return widgets


def _handle_config_init(binary: Any, home: Any, argv: list[str]) -> dict[str, Any]:
    """对话内应用配置的受信路由：不透传 argv，改走加固过的执行路径。

    两种形态：``--new`` 走后台进程（阻塞式浏览器创建流程，URL 接力给
    用户）；``--app-id`` + ``--app-secret`` 走幂等的 ``ensure_configured``
    （secret 截下后经 stdin 注入 CLI 并全程脱敏输出——用户在对话里提供
    的 secret 无法从聊天记录里抹掉，但至少不进 CLI argv 与执行结果）。
    """

    if "--new" in argv:
        status = background.config_init_new_status(binary, home)
        if status.get("status") == "failed":
            return {
                "success": False,
                "error": {
                    "code": "LARK_CLI_CONFIG_INIT_FAILED",
                    "message": str(status.get("detail") or "应用创建失败。"),
                    "retryable": True,
                },
            }
        return {"success": True, "data": status}
    app_id = str(_flag_value(argv, "--app-id") or "").strip()
    app_secret = str(_flag_value(argv, "--app-secret") or "").strip()
    if not app_id or not app_secret:
        return _precondition_failure(
            "LARK_CLI_CONFIG_ARGS_REQUIRED",
            "config init 需要 --app-id 与 --app-secret（或使用 --new 现场创建应用）。",
        )
    brand = str(_flag_value(argv, "--brand") or "feishu").strip() or "feishu"
    try:
        ensure_configured(binary, home, app_id=app_id, app_secret=app_secret, brand=brand)
    except LarkCliRunError as exc:
        return {
            "success": False,
            "error": {"code": exc.code, "message": exc.message, "retryable": True},
        }
    finally:
        app_secret = ""
    return {
        "success": True,
        "data": {
            "configured_app_ids": sorted(configured_app_ids(home)),
            "next_step": (
                "应用凭据已配置。继续调用 auth status 检查用户登录态，"
                "未登录则走设备码登录流程。"
            ),
        },
    }


def _flag_value(argv: list[str], flag: str) -> str | None:
    for index, token in enumerate(argv[:-1]):
        if token == flag:
            return argv[index + 1]
    return None


def _step_authorizes_submission(active_skill: Any | None, active_step_id: str | None) -> bool:
    """结构性流程闸：提交动作必须被当前 SOP 节点显式授权。

    这保证"展示表单 → 用户确认"的暂停节点无法被绕过——conversation 帧
    与未引用 lark_cli 的 SOP 节点里，模型即便持有正确摘要也无法提交。
    """

    if active_skill is None:
        return False
    from app.core.task_request_compiler import current_step_capability_refs

    refs = current_step_capability_refs(active_skill, active_step_id)
    tool_ids = {str(item).strip() for item in refs.get("tool_ids") or []}
    return TOOL_NAME in tool_ids or "builtin.lark_cli" in tool_ids


def _apply_submission_gate(
    db: Any,
    *,
    session_id: str,
    task_frame_id: str,
    arguments: dict[str, Any],
    resolved: policy.ResolvedCommand,
) -> tuple[dict[str, Any] | None, list[str]]:
    """真实提交的确认闸；返回 (错误或 None, 重组后的 argv)。

    两条路径：① 推荐——不带 --data 的零参数提交，受信层重放本会话最近
    一次 dry-run 预览的规范化内容（跨帧后模型无需重组表单，从根上消灭
    自造控件 id / 格式回退类错误；提交内容与用户看到的预览按构造一致）。
    ② 显式携带 --data 时维持原语义：摘要必须与本会话内某条预览记录一致。
    """

    argv = list(resolved.argv)
    confirmed = str(arguments.get("confirmed_form_digest") or "").strip()
    digest = policy.submission_digest(resolved.data_json)
    if digest:
        if not confirmed:
            return (
                _precondition_failure(
                    "LARK_CLI_CONFIRMATION_REQUIRED",
                    "显式携带 --data 提交时缺少 confirmed_form_digest。更简单的"
                    "做法：去掉 --data 与本字段直接调用，系统会自动提交本会话"
                    "最近一次 dry-run 预览（用户已确认）的内容。",
                ),
                argv,
            )
        if confirmed != digest:
            return (
                _precondition_failure(
                    "LARK_CLI_PREVIEW_DIGEST_MISMATCH",
                    "confirmed_form_digest 与本次提交内容不一致：提交体被修改过，"
                    "必须重新 dry-run 预览并让用户确认新内容。",
                ),
                argv,
            )
        if _find_preview_data(db, session_id, digest=digest) is None:
            return (
                _precondition_failure(
                    "LARK_CLI_PREVIEW_NOT_FOUND",
                    "本会话内没有该内容的 dry-run 预览记录：不允许提交未经"
                    "用户预览确认的内容。",
                ),
                argv,
            )
        data_json = policy.wire_submission_data(dict(resolved.data_json or {}))
    else:
        cached = _find_preview_data(db, session_id, digest=confirmed or None)
        canonical = (cached or {}).get("canonical_data") if cached else None
        if not isinstance(canonical, dict):
            return (
                _precondition_failure(
                    "LARK_CLI_PREVIEW_NOT_FOUND",
                    "本会话内没有可重放的 dry-run 预览记录：请先组装 --data 并"
                    "执行 --dry-run 预览、向用户逐字段展示并获得明确确认，"
                    "然后再提交。",
                ),
                argv,
            )
        digest = str(cached.get("submission_digest") or "")
        data_json = policy.wire_submission_data(dict(canonical))
    data_json["uuid"] = _submission_uuid(task_frame_id, digest)
    argv = _replace_flag_value(
        argv, "--data", json.dumps(data_json, ensure_ascii=False, separators=(",", ":"))
    )
    argv = _ensure_flag_pair(argv, "--as", "user")
    argv.append("--yes")
    return None, argv


def _find_preview_data(
    db: Any, session_id: str, digest: str | None = None
) -> dict[str, Any] | None:
    """按时间倒序找本会话内最近一条 dry-run 预览的缓存数据。

    指定 digest 时要求摘要一致（显式 --data 路径）；未指定时取最近一条
    （零参数重放路径——正常流程里进入提交节点时它就是用户刚确认的那次）。
    """

    rows = db.exec(
        select(HarnessInvocationRecord)
        .where(
            HarnessInvocationRecord.session_id == session_id,
            HarnessInvocationRecord.tool_name == TOOL_NAME,
            HarnessInvocationRecord.status == "completed",
        )
        .order_by(HarnessInvocationRecord.started_at.desc())  # type: ignore[attr-defined]
    ).all()
    for row in rows:
        cached = row.response_cache_json or {}
        data = cached.get("data")
        if not isinstance(data, dict) or not data.get("submission_digest"):
            continue
        if digest is not None and data.get("submission_digest") != digest:
            continue
        return data
    return None


def _translate(
    result: Any,
    resolved: policy.ResolvedCommand,
    stashed_digest: str | None,
    stashed_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    envelope = result.envelope
    if result.exit_code == 10:
        # 不应发生：--yes 由本模块注入。出现说明策略表与 CLI 风险面失配。
        return {
            "success": False,
            "error": {
                "code": "LARK_CLI_UNEXPECTED_CONFIRMATION",
                "message": "CLI 要求确认标志但系统未注入——请反馈给管理员检查策略表。",
                "retryable": False,
            },
        }
    # 部分命令（如 auth status）的信封没有 ok 字段：显式 ok 优先，
    # 否则以「退出码 0 且无 error 字段」为成功。
    if isinstance(envelope, dict):
        if "ok" in envelope:
            ok = bool(envelope.get("ok"))
        else:
            ok = result.exit_code == 0 and "error" not in envelope
    else:
        ok = result.exit_code == 0
    if ok:
        data: dict[str, Any] = {"envelope": envelope if envelope is not None else {}}
        if envelope is None and result.stdout.strip():
            data["stdout"] = result.stdout.strip()[:4_000]
        if stashed_digest:
            data["submission_digest"] = stashed_digest
            if stashed_body is not None:
                # 落库供提交节点零参数重放（response_cache_json 持久化的就是
                # 本返回值）；对模型也是一份"已预览内容"的权威快照。
                data["canonical_data"] = stashed_body
            data["next_step"] = (
                "向用户逐字段展示上述表单内容；用户明确确认后，在提交节点直接"
                "调用 approval instances create（不带 --data），系统会自动提交"
                "本次预览的内容。"
            )
        return {"success": True, "data": data}
    error = envelope.get("error") if isinstance(envelope, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "lark-cli 返回错误。")
        hint = str(error.get("hint") or "").strip()
        if hint:
            message = f"{message}（提示：{hint}）"
        subtype = str(error.get("subtype") or error.get("type") or "error")
        code = "LARK_CLI_" + subtype.upper()
        message += _form_error_guidance(message, subtype)
    else:
        tail = (result.stderr or result.stdout).strip()[-800:]
        message = f"lark-cli 执行失败（exit {result.exit_code}）：{tail}"
        code = "LARK_CLI_EXEC_FAILED"
    message += _auth_scopes_error_guidance(resolved.rule.prefix)
    retryable = resolved.rule.action != "gated_write" or resolved.is_dry_run
    return {
        "success": False,
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def _auth_scopes_error_guidance(prefix: tuple[str, ...]) -> str:
    """auth scopes 失败的确定性纠偏（真实案例驱动）。

    该诊断命令自身需要应用管理类权限（admin:app.info:readonly 等），普通
    审批应用未申请时必然失败；错误原文"app has not applied for the
    required scope(s)"曾被模型误读为"应用没有审批权限、流程无法继续"，
    未调一次 auth login 就 finish(failed)，整条 SOP 被暂停。指引必须把
    错误重定向到正确动作，而不是任由模型自行归因。
    """

    if prefix != ("auth", "scopes"):
        return ""
    return (
        "（说明：auth scopes 是诊断命令，本身需要应用管理类权限，普通审批"
        "应用未申请时必然失败；此失败只关乎该诊断命令自身，与审批流程所需"
        "权限无关，更不代表任务失败。请不要依赖此命令：用户未登录时直接"
        "执行 auth login --scope <所需审批 scope> --no-wait 发起设备码"
        "授权即可。）"
    )


def _form_error_guidance(message: str, subtype: str) -> str:
    """把服务端拒绝表单的高频错误翻译成可执行的修正指引（真实案例驱动）。"""

    if "未找到表单控件" in message or "widget" in message.lower():
        return (
            "（控件 id、type 与嵌套结构必须逐一取自 approvals get 返回的 form 定义，"
            "不得自造；复合控件（如 leaveGroupV2）的子控件必须嵌套在该复合控件的 "
            "value 数组内整体提交，不得平铺到 form 顶层。请重新获取定义详情、按其"
            "结构重组 form，然后重新 --dry-run 预览后再提交。）"
        )
    if "RFC3339" in message:
        return (
            "（date 控件的值需为 RFC3339 格式；系统会自动把 YYYY-MM-DD[ HH:MM[:SS]] "
            "等常见写法补全时区，仍报此错说明该值不是可解析的日期字符串——请检查"
            "对应控件的取值，修正后重新 --dry-run 预览再提交。）"
        )
    if "form" in message and ("invalid" in subtype.lower() or "Invalid parameter" in message):
        return (
            "（表单被服务端拒绝的常见原因：radioV2/select 的 value 必须用 "
            "approvals get 返回的选项 key 而非选项文字；请假类定义若含 "
            "leaveGroupV2 复合控件需按其结构整体组装。修正后必须重新 "
            "--dry-run 预览并再次征得用户确认。）"
        )
    return ""


def _submission_uuid(task_frame_id: str, digest: str) -> str:
    seed = f"{task_frame_id}:{digest}".encode()
    return hashlib.sha256(seed).hexdigest()[:32]


def _feishu_binding(db: Any, tenant_id: str, agent_id: str | None) -> ChannelBinding | None:
    rows = db.exec(
        select(ChannelBinding).where(
            ChannelBinding.tenant_id == tenant_id,
            ChannelBinding.channel == "feishu",
            ChannelBinding.status == "active",
        )
    ).all()
    if not rows:
        return None
    if agent_id:
        for row in rows:
            if row.agent_id == agent_id:
                return row
    return rows[0]


def _ensure_flag_pair(argv: list[str], flag: str, value: str) -> list[str]:
    if flag in argv:
        return argv
    return [*argv, flag, value]


def _replace_flag_value(argv: list[str], flag: str, value: str) -> list[str]:
    result = list(argv)
    for index, token in enumerate(result[:-1]):
        if token == flag:
            result[index + 1] = value
            return result
    return [*result, flag, value]


def _precondition_failure(subcode: str, message: str) -> dict[str, Any]:
    # 主码固定 INVALID_ARGUMENTS：见模块 docstring 的防重放声明约定。
    return {
        "success": False,
        "error": {
            "code": "INVALID_ARGUMENTS",
            "subcode": subcode,
            "message": message,
            "retryable": True,
        },
    }
