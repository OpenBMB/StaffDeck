"""Agent Skills 官方规范的落地实现(agentskills.io/specification)。

规范要点:
- 技能 = 目录,至少含 SKILL.md(YAML frontmatter + Markdown 正文);
- frontmatter:name(必填,1-64,小写字母/数字/连字符,不可首尾/连续连字符,
  须与目录名一致)、description(必填,1-1024,做什么+何时用);
  可选 license / compatibility(≤500) / metadata(键值对) / allowed-tools(空格分隔);
- 可选目录 scripts/ references/ assets/;渐进披露。

本模块提供校验、frontmatter 解析与 SKILL.md 组装,后端各处保存/导出/运行统一收口。
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# name:1-64,小写字母/数字/连字符,不可首尾连字符,不可连续连字符
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_NAME_MAX = 64
SKILL_DESCRIPTION_MAX = 1024
SKILL_COMPATIBILITY_MAX = 500
# frontmatter 中透传的可选标量字段
_OPTIONAL_SCALAR_KEYS = ("license", "compatibility", "allowed-tools")


def validate_skill_name(name: str) -> str | None:
    """校验规范 name 字段,返回错误信息;合法返回 None。"""
    if not name or len(name) > SKILL_NAME_MAX:
        return f"name 必填且不超过 {SKILL_NAME_MAX} 字符"
    if not SKILL_NAME_PATTERN.fullmatch(name):
        return "name 只能包含小写字母、数字和连字符,且不可首尾或连续使用连字符"
    return None


def validate_skill_description(description: str, *, required: bool) -> str | None:
    """校验规范 description 字段;required 时必填,非必填时允许为空但限长。"""
    text = (description or "").strip()
    if not text:
        return "description 必填(描述技能做什么、什么时候使用)" if required else None
    if len(text) > SKILL_DESCRIPTION_MAX:
        return f"description 不能超过 {SKILL_DESCRIPTION_MAX} 字符"
    return None


def validate_skill_compatibility(compatibility: str) -> str | None:
    """校验规范 compatibility 字段(可选,≤500 字符)。"""
    text = (compatibility or "").strip()
    if len(text) > SKILL_COMPATIBILITY_MAX:
        return f"compatibility 不能超过 {SKILL_COMPATIBILITY_MAX} 字符"
    return None


def split_frontmatter(markdown: str, *, strict: bool = False) -> tuple[dict[str, Any], str]:
    """拆出 YAML frontmatter 与正文;无 frontmatter 时返回 ({}, 原文)。

    使用真正的 YAML 解析(PyYAML safe_load),重复保存不会累积转义。
    strict=True 时,frontmatter 未闭合/YAML 非法/顶层不是映射 均抛 ValueError
    (保存路径使用);strict=False(读路径)遇到非法 frontmatter 回退 ({}, 原文)。
    """
    text = markdown or ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    closing = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing = index
            break
    if closing is None:
        if strict:
            raise ValueError("SKILL.md 的 YAML frontmatter 未闭合(缺少结束 --- 行)")
        return {}, text
    raw = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1:]).lstrip("\n")
    try:
        parsed = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        if strict:
            raise ValueError(f"SKILL.md 的 YAML frontmatter 非法:{exc}") from exc
        return {}, text
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        if strict:
            raise ValueError("SKILL.md 的 YAML frontmatter 顶层必须是键值映射")
        return {}, text
    return parsed, body


def _dump_frontmatter(fields: dict[str, Any]) -> str:
    """用真实 YAML 序列化输出 frontmatter(保序、Unicode 原文、自动转义)。"""
    return yaml.safe_dump(
        fields,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=10**6,
    ).strip()


def compose_skill_markdown(
    *,
    name: str,
    description: str,
    body: str,
    license: str = "",
    compatibility: str = "",
    allowed_tools: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """按规范组装 SKILL.md(frontmatter + 正文);metadata 中的保留键被忽略。"""
    fields: dict[str, Any] = {"name": name, "description": description}
    optional = {
        "license": license.strip(),
        "compatibility": compatibility.strip(),
        "allowed-tools": allowed_tools.strip(),
    }
    for key in _OPTIONAL_SCALAR_KEYS:
        if optional[key]:
            fields[key] = optional[key]
    extra_metadata = {
        str(key): value
        for key, value in (metadata or {}).items()
        if str(key) not in {"name", "description", *_OPTIONAL_SCALAR_KEYS} and str(key).strip()
    }
    if extra_metadata:
        fields["metadata"] = extra_metadata
    return "---\n" + _dump_frontmatter(fields) + "\n---\n\n" + (body or "").strip() + "\n"


def frontmatter_for_skill(skill: Any) -> dict[str, Any]:
    """从 GeneralSkill 行组装规范 frontmatter 字段(name 取 slug,与目录名一致)。"""
    metadata, _ = split_frontmatter(getattr(skill, "skill_markdown", "") or "")
    return {
        "name": skill.slug,
        "description": (skill.description or "").strip(),
        "license": str(metadata.get("license") or ""),
        "compatibility": str(metadata.get("compatibility") or ""),
        "allowed_tools": str(metadata.get("allowed-tools") or ""),
        "metadata": metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {},
    }


def standard_skill_markdown(skill: Any) -> str:
    """返回规范化后的完整 SKILL.md:frontmatter 以表单/数据库字段为准重组,正文保留。

    存量技能(无 frontmatter 或字段缺失)在读路径同样输出规范形态。
    """
    fields = frontmatter_for_skill(skill)
    _, body = split_frontmatter(getattr(skill, "skill_markdown", "") or "")
    return compose_skill_markdown(body=body, **fields)


def standard_package_files(skill: Any) -> list[dict[str, Any]]:
    """导出/物化用的标准文件清单:SKILL.md 为规范化版本,其余文件原样(去重 SKILL.md)。"""
    files = [
        {
            "path": "SKILL.md",
            "content": standard_skill_markdown(skill),
            "mime_type": "text/markdown",
        }
    ]
    for file in getattr(skill, "skill_files_json", None) or []:
        path = str(file.get("path") or "").strip()
        if not path or path.upper() == "SKILL.MD":
            continue
        files.append(
            {
                "path": path,
                "content": str(file.get("content") or ""),
                "mime_type": file.get("mime_type") or "text/plain",
            }
        )
    return files


def allowed_tools_list(skill: Any) -> list[str]:
    """从 frontmatter 解析 allowed-tools 声明(空格分隔);未声明返回空列表。"""
    fields = frontmatter_for_skill(skill)
    raw = fields.get("allowed_tools") or ""
    return [item for item in raw.split() if item]
