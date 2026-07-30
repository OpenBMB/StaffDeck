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


def split_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    """拆出 YAML frontmatter 与正文;无 frontmatter 时返回 ({}, 原文)。

    轻量解析:支持标量、键值嵌套(metadata:)与简单列表,与既有导入解析口径一致。
    """
    lines = (markdown or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, markdown or ""
    metadata: dict[str, Any] = {}
    body_start = len(lines)
    current_map_key = ""
    for index, line in enumerate(lines[1:], start=1):
        stripped = line.strip()
        if stripped == "---":
            body_start = index + 1
            break
        if not stripped or stripped.startswith("#"):
            continue
        # metadata: 下的嵌套键值(缩进)
        if line.startswith((" ", "\t")) and current_map_key and ":" in stripped:
            key, value = stripped.split(":", 1)
            key = key.strip()
            if key and isinstance(metadata.get(current_map_key), dict):
                metadata[current_map_key][key] = _parse_value(value.strip())
            continue
        current_map_key = ""
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if not value:
            # 可能是嵌套映射(如 metadata:)——先占位字典
            metadata[key] = {}
            current_map_key = key
        else:
            metadata[key] = _parse_value(value)
    return metadata, "\n".join(lines[body_start:]).lstrip("\n")


def _parse_value(value: str) -> Any:
    cleaned = value.strip().strip("'\"")
    if cleaned.startswith("[") and cleaned.endswith("]"):
        return [item.strip().strip("'\"") for item in cleaned[1:-1].split(",") if item.strip()]
    return cleaned


def _yaml_scalar(value: str) -> str:
    """输出安全的 YAML 标量(含特殊字符时加引号)。"""
    text = str(value)
    if re.search(r"[:#\[\]{}&*!|>'\"%@`\s]", text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


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
    lines = ["---", f"name: {_yaml_scalar(name)}", f"description: {_yaml_scalar(description)}"]
    optional = {
        "license": license.strip(),
        "compatibility": compatibility.strip(),
        "allowed-tools": allowed_tools.strip(),
    }
    for key in _OPTIONAL_SCALAR_KEYS:
        if optional[key]:
            lines.append(f"{key}: {_yaml_scalar(optional[key])}")
    extra_metadata = {
        str(key): value
        for key, value in (metadata or {}).items()
        if str(key) not in {"name", "description", *_OPTIONAL_SCALAR_KEYS} and str(key).strip()
    }
    if extra_metadata:
        lines.append("metadata:")
        for key, value in extra_metadata.items():
            lines.append(f"  {key}: {_yaml_scalar(value)}")
    lines.append("---")
    lines.append("")
    lines.append((body or "").strip())
    return "\n".join(lines).rstrip("\n") + "\n"


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
