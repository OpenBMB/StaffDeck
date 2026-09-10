"""lark-cli 子命令策略表。

分层策略（读宽写严）：

- 显式登记的**读命令**放行任意 flag（CLI 官方 typed 参数如 ``--approval-code``
  直接可用——曾因白名单过窄拦下官方正确用法，逼模型反复试错）；仅保留
  硬约束：禁 ``--yes``、禁 ``@file``/stdin 值引用、``--as`` 只能 user。
- **未登记的命令**返回 ``needs_risk_probe`` 候选，由 service 层跑
  ``--help`` 读取 CLI 自带的 ``Risk:`` 分级：``read`` 放行，写类拒绝。
- **写命令**维持严格白名单；``--yes`` 与 ``uuid`` 一律由受信代码管理。
- ``api`` 原始逃生舱（可发任意请求）永久封禁；``config`` 域仅放行
  ``init``（对话内配置凭据 / ``--new`` 现场创建应用），由 service 特殊
  路由执行。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime


class LarkCliPolicyError(Exception):
    """argv 被策略拒绝；``code`` 会透传给模型作为可恢复错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CommandRule:
    prefix: tuple[str, ...]
    action: str  # "read" | "write" | "gated_write"
    allowed_flags: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: float = 60.0


_OUTPUT_FLAGS = frozenset({"--format", "--json", "--jq", "-q"})
_DATA_FLAGS = frozenset({"--data", "--params"})

_RULES: tuple[CommandRule, ...] = (
    CommandRule(("auth", "status"), "read", _OUTPUT_FLAGS),
    CommandRule(("auth", "check"), "read", _OUTPUT_FLAGS | {"--scope"}),
    # 免确认写名单的收录标准：仅限"纯本地态操作"——只动本机 token/配置、
    # 不触达任何他人、不在租户内产生可见副作用（auth login/logout、config
    # init）。CLI 的 Risk: write 档里混有真实外部副作用的命令（如 im
    # messages send 发消息、+chat-create 建群），那些永远不进本名单——
    # 未来若要开放，走"用户确认后执行"的通用确认闸，不是加名单。
    CommandRule(
        ("auth", "login"),
        "write",
        _OUTPUT_FLAGS
        | {"--scope", "--domain", "--recommend", "--exclude", "--no-wait", "--device-code"},
        timeout_seconds=120.0,
    ),
    CommandRule(("auth", "logout"), "write", _OUTPUT_FLAGS),
    CommandRule(("auth", "scopes"), "read", _OUTPUT_FLAGS),
    # --app-secret 是工具层约定的虚拟 flag：CLI 实际只收 --app-secret-stdin，
    # service 会截下值经 stdin 注入并做输出脱敏。--force-init 刻意不放行。
    CommandRule(
        ("config", "init"),
        "write",
        _OUTPUT_FLAGS | {"--new", "--app-id", "--app-secret", "--brand", "--lang"},
        timeout_seconds=90.0,
    ),
    CommandRule(
        ("approval", "approvals", "search"), "read", _OUTPUT_FLAGS | _DATA_FLAGS | {"--as"}
    ),
    CommandRule(
        ("approval", "approvals", "get"), "read", _OUTPUT_FLAGS | _DATA_FLAGS | {"--as"}
    ),
    CommandRule(
        ("approval", "instances", "create"),
        "gated_write",
        _OUTPUT_FLAGS | _DATA_FLAGS | {"--as", "--dry-run"},
    ),
    CommandRule(("approval", "instances", "get"), "read", _OUTPUT_FLAGS | _DATA_FLAGS | {"--as"}),
    CommandRule(
        ("approval", "instances", "initiated"),
        "read",
        _OUTPUT_FLAGS | _DATA_FLAGS | {"--as"},
    ),
    CommandRule(("approval", "tasks", "query"), "read", _OUTPUT_FLAGS | _DATA_FLAGS | {"--as"}),
    CommandRule(("schema",), "read", _OUTPUT_FLAGS),
    CommandRule(("skills", "list"), "read", _OUTPUT_FLAGS),
    CommandRule(("skills", "read"), "read", _OUTPUT_FLAGS),
)

_MAX_ARGS = 32
_MAX_ARG_CHARS = 8_000
_HELP_ALLOWED_DOMAINS = {"auth", "approval", "schema", "skills", "config"}


@dataclass(frozen=True)
class ResolvedCommand:
    rule: CommandRule
    argv: tuple[str, ...]
    is_dry_run: bool
    is_help: bool
    data_json: dict[str, object] | None
    # 未登记命令的动态候选：需要 service 层用 CLI 自带的 Risk 分级裁决。
    needs_risk_probe: bool = False

    @property
    def is_side_effect_write(self) -> bool:
        if self.rule.action == "read" or self.is_help:
            return False
        return not self.is_dry_run


def resolve(args: list[str]) -> ResolvedCommand:
    """校验模型传入的 argv 并解析出策略裁决。"""

    if not args:
        raise LarkCliPolicyError("LARK_CLI_EMPTY_COMMAND", "args 不能为空。")
    if len(args) > _MAX_ARGS:
        raise LarkCliPolicyError("LARK_CLI_TOO_MANY_ARGS", f"args 数量超过 {_MAX_ARGS}。")
    normalized: list[str] = []
    for item in args:
        text = str(item or "")
        if not text.strip():
            raise LarkCliPolicyError("LARK_CLI_BLANK_ARG", "args 不允许包含空白项。")
        if len(text) > _MAX_ARG_CHARS:
            raise LarkCliPolicyError(
                "LARK_CLI_ARG_TOO_LONG", f"单个参数超过 {_MAX_ARG_CHARS} 字符。"
            )
        if "\x00" in text or "\n" in text or "\r" in text:
            raise LarkCliPolicyError("LARK_CLI_ILLEGAL_CHARS", "参数包含非法控制字符。")
        normalized.append(text)

    rule = _match_rule(normalized)
    if rule is None:
        # 允许对白名单域的任意层级查看 --help（如 `approval instances --help`）：
        # 纯只读，帮模型现场自查用法，避免试错烧预算。
        if (
            normalized[0] in _HELP_ALLOWED_DOMAINS
            and normalized[-1] in {"--help", "-h"}
            and all(not item.startswith("-") for item in normalized[:-1])
        ):
            help_rule = CommandRule(tuple(normalized[:-1]), "read", frozenset({"--help"}))
            return ResolvedCommand(
                rule=help_rule,
                argv=tuple(normalized),
                is_dry_run=False,
                is_help=True,
                data_json=None,
            )
        return _resolve_dynamic_candidate(normalized)
    if rule.action == "read":
        flag_values = _validate_relaxed(normalized[len(rule.prefix):])
    else:
        flag_values = _validate_flags(normalized[len(rule.prefix):], rule)
    data_json = (
        _parse_data_json(flag_values.get("--data"))
        if rule.action == "gated_write"
        else None
    )
    return ResolvedCommand(
        rule=rule,
        argv=tuple(normalized),
        is_dry_run="--dry-run" in flag_values,
        is_help="--help" in flag_values,
        data_json=data_json,
    )


def _resolve_dynamic_candidate(normalized: list[str]) -> ResolvedCommand:
    """未登记命令：交给 service 层按 CLI 的 ``Risk:`` 分级动态裁决。

    ``api``（任意请求逃生舱）与 ``config`` 其余子命令永远不进入动态通道。
    """

    domain = normalized[0]
    if domain in {"api", "config"}:
        raise LarkCliPolicyError(
            "LARK_CLI_COMMAND_BLOCKED",
            "该 lark-cli 子命令被封禁（api 原始逃生舱与 config 其余子命令不开放）。",
        )
    prefix: list[str] = []
    for token in normalized:
        if token.startswith("-"):
            break
        prefix.append(token)
    if not prefix:
        raise LarkCliPolicyError(
            "LARK_CLI_COMMAND_BLOCKED", "无法识别的 lark-cli 命令形式。"
        )
    if tuple(prefix[:2]) == ("auth", "qrcode"):
        # 真实案例：模型每次登录都先试 qrcode 渲染二维码，白烧一轮才回退
        # 发链接。与其走 risk-probe 拒绝，不如直接给出正确动作。
        raise LarkCliPolicyError(
            "LARK_CLI_COMMAND_BLOCKED",
            "auth qrcode（终端二维码渲染）在托管环境不开放：控制台无法展示"
            "本地生成的二维码，也不需要二维码——把 auth login 返回的 "
            "verification_url 链接原样发给用户点击打开即可。",
        )
    flag_values = _validate_relaxed(normalized[len(prefix):])
    return ResolvedCommand(
        rule=CommandRule(tuple(prefix), "read", frozenset()),
        argv=tuple(normalized),
        is_dry_run="--dry-run" in flag_values,
        is_help="--help" in flag_values,
        data_json=None,
        needs_risk_probe=True,
    )


def _validate_relaxed(rest: list[str]) -> dict[str, str | None]:
    """读命令的宽松校验：任意 flag 透传，仅执行硬约束。

    硬约束：禁 ``--yes``（确认标志只能由受信代码注入）、禁 ``@file`` 与
    ``-``（stdin）值引用（防本地文件外带）、``--as`` 只能取 ``user``。
    """

    values: dict[str, str | None] = {}
    previous_flag: str | None = None
    for token in rest:
        if token in {"--help", "-h"}:
            values["--help"] = None
            previous_flag = None
            continue
        if token in {"--yes", "-y"}:
            raise LarkCliPolicyError(
                "LARK_CLI_YES_NOT_ALLOWED",
                "禁止自带 --yes：高危提交的确认标志由系统在用户确认后注入。",
            )
        if token.startswith("-") and len(token) > 1:
            values[token] = None
            previous_flag = token
            continue
        if token == "-" or token.startswith("@"):
            raise LarkCliPolicyError(
                "LARK_CLI_FILE_REF_BLOCKED",
                "参数值不允许 @file 或 - (stdin) 引用，请内联内容。",
            )
        if previous_flag is not None:
            values[previous_flag] = token
            previous_flag = None
        # 无前置 flag 的位置参数直接透传，由 CLI 校验。
    as_value = values.get("--as")
    if "--as" in values and as_value not in (None, "user"):
        raise LarkCliPolicyError(
            "LARK_CLI_IDENTITY_BLOCKED", "审批命令只允许 --as user 身份执行。"
        )
    return values


def canonical_submission_body(data_json: dict[str, object] | None) -> dict[str, object] | None:
    """提交体的语义规范形：``form`` 统一解析为结构（数组/字符串编码等价），
    日期值统一规范化为 RFC3339（写法差异不改变语义摘要）。

    飞书 API 要求 form 是 JSON 字符串，但模型常传数组——两种编码语义相同，
    摘要必须一致，否则"编码自修正"会被确认闸误杀（真实案例）。
    """

    if not isinstance(data_json, dict) or not data_json:
        return None
    body: dict[str, object] = {
        key: value for key, value in data_json.items() if key != "uuid"
    }
    structure = _parsed_form_structure(body.get("form"))
    if structure is not None:
        body["form"] = structure
    return body


def wire_submission_data(data_json: dict[str, object]) -> dict[str, object]:
    """转成 CLI/API 线上格式：``form`` 序列化为 JSON 字符串（官方要求），
    日期值规范化为 RFC3339。

    审批定义详情把 date 控件展示为 ``YYYY-MM-DD hh:mm``，模型照抄该格式
    组装 form 后提交会被服务端以 "not RFC3339" 拒绝（真实案例）——定义
    自身的展示格式与提交格式不一致，只能由受信层兜底转换。
    """

    wire = dict(data_json)
    structure = _parsed_form_structure(wire.get("form"))
    if structure is not None:
        wire["form"] = json.dumps(structure, ensure_ascii=False, separators=(",", ":"))
    return wire


def _parsed_form_structure(form: object) -> object | None:
    """form 的规范结构：字符串先解析，再递归规范化日期；非 form 内容返回 None。"""

    if isinstance(form, str):
        try:
            structure = json.loads(form)
        except json.JSONDecodeError as exc:
            raise LarkCliPolicyError(
                "LARK_CLI_FORM_INVALID_JSON", f"form 字符串不是合法 JSON：{exc}"
            )
    elif isinstance(form, (list, dict)):
        structure = copy.deepcopy(form)
    else:
        return None
    _normalize_form_dates(structure)
    return structure


def _normalize_form_dates(structure: object) -> None:
    """原地规范化控件树里的日期值：date 控件与 dateInterval 的 start/end。

    复合控件（leaveGroupV2、fieldList 等）的 value 是嵌套控件数组，递归处理。
    无法解析的值原样保留，交给服务端报错。
    """

    controls = structure if isinstance(structure, list) else [structure]
    for control in controls:
        if not isinstance(control, dict):
            continue
        control_type = str(control.get("type") or "")
        value = control.get("value")
        if control_type == "date" and isinstance(value, str):
            control["value"] = _rfc3339(value)
        elif control_type == "dateInterval" and isinstance(value, dict):
            for key in ("start", "end"):
                if isinstance(value.get(key), str):
                    value[key] = _rfc3339(value[key])
        elif isinstance(value, list):
            _normalize_form_dates(value)


def _rfc3339(text: str) -> str:
    """把常见日期写法（YYYY-MM-DD [HH:MM[:SS]]、ISO 无时区、Z 后缀等）
    规范化为带本机时区偏移的 RFC3339；解析失败原样返回。"""

    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat(timespec="seconds")


def validate_form_against_definition(
    form: object, definition_widgets: object
) -> list[str]:
    """dry-run 前的表单结构校验：控件 id/type/嵌套/选项 key 必须与定义一致。

    背景：CLI 的 ``--dry-run`` 只本地打印请求、不打服务端——编造的控件
    结构能一路通过预览、被用户确认，直到真实提交才被服务端打回（真实
    案例：把复合控件的子控件 ``widgetLeaveGroupType`` 平铺到 form 顶层）。
    受信层用定义原文把这类错误拦在预览之前。只报确定性错误；缺省控件等
    宽松处交给服务端裁决。返回错误列表，空即通过。
    """

    if not isinstance(form, list) or not isinstance(definition_widgets, list):
        return []
    # 定义索引：控件 id → (定义节点, 父复合控件 id 或 None)
    index: dict[str, tuple[dict[str, object], str | None]] = {}

    def _walk(widgets: list[object], parent: str | None) -> None:
        for widget in widgets:
            if not isinstance(widget, dict) or not widget.get("id"):
                continue
            widget_id = str(widget["id"])
            index.setdefault(widget_id, (widget, parent))
            value = widget.get("value")
            if isinstance(value, list) and any(
                isinstance(child, dict) and child.get("id") for child in value
            ):
                _walk(value, widget_id)

    _walk(definition_widgets, None)
    errors: list[str] = []

    def _check(controls: list[object], parent: str | None) -> None:
        for control in controls:
            if not isinstance(control, dict):
                continue
            control_id = str(control.get("id") or "")
            if not control_id:
                errors.append("存在缺少 id 的控件。")
                continue
            entry = index.get(control_id)
            if entry is None:
                errors.append(
                    f"控件 id {control_id!r} 在审批定义中不存在"
                    "（控件 id 必须逐一取自 approvals get 返回的定义，严禁自造）。"
                )
                continue
            spec, expected_parent = entry
            if expected_parent != parent:
                if expected_parent and not parent:
                    errors.append(
                        f"控件 {control_id!r} 是复合控件 {expected_parent!r} 的"
                        f"子控件，必须嵌套在 {expected_parent!r} 的 value 数组内"
                        "整体提交，不得平铺到 form 顶层。"
                    )
                else:
                    errors.append(
                        f"控件 {control_id!r} 的嵌套位置与定义不符"
                        f"（定义中其父级为 {expected_parent or 'form 顶层'}）。"
                    )
            spec_type = str(spec.get("type") or "")
            control_type = str(control.get("type") or "")
            if spec_type and control_type and control_type != spec_type:
                errors.append(
                    f"控件 {control_id!r} 的 type 应为 {spec_type!r}，"
                    f"实际是 {control_type!r}。"
                )
            value = control.get("value")
            option = spec.get("option")
            if isinstance(option, list) and option:
                keys = {
                    str(item.get("value"))
                    for item in option
                    if isinstance(item, dict)
                }
                by_text = {
                    str(item.get("text")): str(item.get("value"))
                    for item in option
                    if isinstance(item, dict)
                }
                candidates = (
                    [value]
                    if isinstance(value, str)
                    else [v for v in value if isinstance(v, str)]
                    if isinstance(value, list)
                    else []
                )
                for candidate in candidates:
                    if candidate not in keys:
                        hint = (
                            f"；若想选 {candidate!r}，对应 key 是 "
                            f"{by_text[candidate]!r}"
                            if candidate in by_text
                            else ""
                        )
                        errors.append(
                            f"控件 {control_id!r} 的取值必须用选项 key"
                            f"（可选：{sorted(keys)}），不是选项文字{hint}。"
                        )
            spec_children = spec.get("value")
            if isinstance(spec_children, list) and any(
                isinstance(child, dict) and child.get("id")
                for child in spec_children
            ):
                if isinstance(value, list):
                    _check(value, control_id)
                else:
                    errors.append(
                        f"复合控件 {control_id!r} 的 value 必须是子控件数组。"
                    )

    _check(form, None)
    return errors


def submission_digest(data_json: dict[str, object] | None) -> str | None:
    """对提交体的语义规范形做摘要；编码差异不改变摘要，内容差异必然改变。"""

    body = canonical_submission_body(data_json)
    if body is None:
        return None
    canonical = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def logical_write_signature(arguments: dict[str, object]) -> str | None:
    """真实提交（gated_write 且非 dry-run）的防重放签名，其余返回 None。

    ``auth login`` 虽是写操作，但设备码轮询天然需要重试且无外部副作用
    风险，纳入防重放反而会把合法重试挡死，故刻意排除。
    """

    raw_args = arguments.get("args")
    if not isinstance(raw_args, list):
        return None
    try:
        resolved = resolve([str(item) for item in raw_args])
    except LarkCliPolicyError:
        return None
    if resolved.rule.action != "gated_write" or not resolved.is_side_effect_write:
        return None
    try:
        digest = submission_digest(resolved.data_json) or ""
    except LarkCliPolicyError:
        return None
    canonical = json.dumps(
        {
            "prefix": list(resolved.rule.prefix),
            "digest": digest,
            # 语义签名不含 argv：同一提交内容无论编码/旗标顺序都命中同一声明。
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _match_rule(argv: list[str]) -> CommandRule | None:
    for rule in _RULES:
        if tuple(argv[: len(rule.prefix)]) == rule.prefix:
            return rule
    return None


def _validate_flags(rest: list[str], rule: CommandRule) -> dict[str, str | None]:
    """校验前缀之后的 token：只允许规则声明的 flag 及其值。"""

    values: dict[str, str | None] = {}
    index = 0
    # --help 对任何白名单命令都放行：纯只读，让模型能现场自查用法。
    boolean_flags = {"--json", "--dry-run", "--no-wait", "--recommend", "--new", "--help", "-h"}
    while index < len(rest):
        token = rest[index]
        if token in {"--help", "-h"}:
            values["--help"] = None
            index += 1
            continue
        if token in {"--yes", "-y"}:
            raise LarkCliPolicyError(
                "LARK_CLI_YES_NOT_ALLOWED",
                "禁止自带 --yes：高危提交的确认标志由系统在用户确认后注入。",
            )
        if not token.startswith("-"):
            # schema/skills 允许一个位置参数（如 schema approval.instances.create）。
            if rule.prefix[0] in {"schema", "skills"} and "positional" not in values:
                values["positional"] = token
                index += 1
                continue
            raise LarkCliPolicyError(
                "LARK_CLI_UNEXPECTED_ARG", f"命令不接受位置参数：{token!r}。"
            )
        if token not in rule.allowed_flags:
            raise LarkCliPolicyError(
                "LARK_CLI_FLAG_BLOCKED", f"flag {token!r} 不在该命令的允许列表内。"
            )
        if token in boolean_flags:
            values[token] = None
            index += 1
            continue
        if index + 1 >= len(rest):
            raise LarkCliPolicyError("LARK_CLI_FLAG_VALUE_MISSING", f"{token} 缺少值。")
        value = rest[index + 1]
        if token in _DATA_FLAGS and (value.startswith("@") or value == "-"):
            raise LarkCliPolicyError(
                "LARK_CLI_FILE_REF_BLOCKED",
                "--data/--params 不允许 @file 或 - (stdin) 引用，请内联 JSON。",
            )
        if token == "--as" and value != "user":
            raise LarkCliPolicyError(
                "LARK_CLI_IDENTITY_BLOCKED", "审批命令只允许 --as user 身份执行。"
            )
        values[token] = value
        index += 2
    return values


def _parse_data_json(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LarkCliPolicyError("LARK_CLI_DATA_INVALID_JSON", f"--data 不是合法 JSON：{exc}")
    if not isinstance(parsed, dict):
        raise LarkCliPolicyError("LARK_CLI_DATA_NOT_OBJECT", "--data 必须是 JSON object。")
    if "uuid" in parsed:
        raise LarkCliPolicyError(
            "LARK_CLI_UUID_NOT_ALLOWED",
            "禁止自带 uuid：幂等标识由系统生成，防止绕过防重放。",
        )
    return parsed
