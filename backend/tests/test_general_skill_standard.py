"""Agent Skills 规范层单测:name/description 校验、frontmatter 解析与组装、标准化输出。"""

from types import SimpleNamespace

from app.general_skills.standard import (
    allowed_tools_list,
    compose_skill_markdown,
    frontmatter_for_skill,
    split_frontmatter,
    standard_package_files,
    standard_skill_markdown,
    validate_skill_description,
    validate_skill_name,
)


def test_validate_skill_name_spec_rules() -> None:
    assert validate_skill_name("pdf-processing") is None
    assert validate_skill_name("a") is None
    assert validate_skill_name("data-analysis-2") is None
    assert validate_skill_name("") is not None
    assert validate_skill_name("x" * 65) is not None
    assert validate_skill_name("PDF-Processing") is not None  # 大写不允许
    assert validate_skill_name("-pdf") is not None  # 首连字符
    assert validate_skill_name("pdf-") is not None  # 尾连字符
    assert validate_skill_name("pdf--processing") is not None  # 连续连字符
    assert validate_skill_name("pdf_processing") is not None  # 下划线
    assert validate_skill_name("pdf processing") is not None  # 空格


def test_validate_skill_description_rules() -> None:
    assert validate_skill_description("做什么、何时用", required=True) is None
    assert validate_skill_description("", required=True) is not None
    assert validate_skill_description("", required=False) is None
    assert validate_skill_description("x" * 1024, required=True) is None
    assert validate_skill_description("x" * 1025, required=True) is not None


def test_split_frontmatter_full_fields() -> None:
    markdown = (
        "---\n"
        "name: pdf-processing\n"
        "description: Extract PDF text. Use when handling PDFs.\n"
        "license: Apache-2.0\n"
        "compatibility: Requires git, docker\n"
        "allowed-tools: Bash(git:*) Read\n"
        "metadata:\n"
        "  author: example-org\n"
        "  version: \"1.0\"\n"
        "---\n"
        "\n"
        "# 正文标题\n"
        "正文内容\n"
    )
    metadata, body = split_frontmatter(markdown)
    assert metadata["name"] == "pdf-processing"
    assert metadata["description"].startswith("Extract PDF text")
    assert metadata["license"] == "Apache-2.0"
    assert metadata["compatibility"] == "Requires git, docker"
    assert metadata["allowed-tools"] == "Bash(git:*) Read"
    assert metadata["metadata"] == {"author": "example-org", "version": "1.0"}
    assert body.startswith("# 正文标题")


def test_split_frontmatter_absent_returns_original() -> None:
    metadata, body = split_frontmatter("# 没有 frontmatter\n正文")
    assert metadata == {}
    assert body.startswith("# 没有 frontmatter")


def test_compose_and_split_roundtrip() -> None:
    composed = compose_skill_markdown(
        name="weather-zh",
        description="查询天气。当用户问天气时使用。",
        body="# 天气技能\n按步骤查询。",
        license="Apache-2.0",
        compatibility="Requires network",
        allowed_tools="Bash(curl:*) Read",
        metadata={"author": "staffdeck", "version": "1.0"},
    )
    metadata, body = split_frontmatter(composed)
    assert metadata["name"] == "weather-zh"
    assert metadata["description"] == "查询天气。当用户问天气时使用。"
    assert metadata["license"] == "Apache-2.0"
    assert metadata["compatibility"] == "Requires network"
    assert metadata["allowed-tools"] == "Bash(curl:*) Read"
    assert metadata["metadata"]["author"] == "staffdeck"
    assert body == "# 天气技能\n按步骤查询。"


def test_compose_escapes_special_scalars() -> None:
    composed = compose_skill_markdown(name="a-b", description='含: 冒号与 "引号"', body="x")
    metadata, _ = split_frontmatter(composed)
    assert metadata["name"] == "a-b"
    assert "冒号" in metadata["description"]


def _skill(**overrides) -> SimpleNamespace:
    base = {
        "slug": "weather-zh",
        "description": "查询天气",
        "skill_markdown": "# 旧正文\n没有 frontmatter。",
        "skill_files_json": [
            {"path": "SKILL.md", "content": "旧的", "mime_type": "text/markdown"},
            {"path": "scripts/run.py", "content": "print(1)", "mime_type": "text/plain"},
        ],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_standard_skill_markdown_synthesizes_frontmatter() -> None:
    skill = _skill()
    markdown = standard_skill_markdown(skill)
    metadata, body = split_frontmatter(markdown)
    # name 恒等于 slug(规范:与目录名一致);正文保留
    assert metadata["name"] == "weather-zh"
    assert metadata["description"] == "查询天气"
    assert body.startswith("# 旧正文")


def test_standard_package_files_replaces_skill_md_and_keeps_rest() -> None:
    files = standard_package_files(_skill())
    assert files[0]["path"] == "SKILL.md"
    assert "name: weather-zh" in files[0]["content"]
    # 旧 SKILL.md 被规范化版本替换,scripts 保留
    assert [file["path"] for file in files] == ["SKILL.md", "scripts/run.py"]


def test_allowed_tools_list() -> None:
    skill = _skill(
        skill_markdown="---\nname: a-b\ndescription: x\nallowed-tools: Bash(git:*) Read\n---\n正文"
    )
    assert allowed_tools_list(skill) == ["Bash(git:*)", "Read"]
    assert allowed_tools_list(_skill()) == []


def test_frontmatter_for_skill_reads_existing_optional_fields() -> None:
    skill = _skill(
        skill_markdown=(
            "---\nname: old\ndescription: old\nlicense: MIT\ncompatibility: Needs docker\n"
            "allowed-tools: Bash\nmetadata:\n  author: me\n---\n正文"
        )
    )
    fields = frontmatter_for_skill(skill)
    assert fields["name"] == "weather-zh"  # 以 slug 为准,不采信旧 name
    assert fields["license"] == "MIT"
    assert fields["compatibility"] == "Needs docker"
    assert fields["allowed_tools"] == "Bash"
    assert fields["metadata"] == {"author": "me"}
