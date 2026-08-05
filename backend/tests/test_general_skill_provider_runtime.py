from app.capabilities.local_general_skill import (
    package_from_row,
)
from app.db.models import GeneralSkill


class RecordingCatalog:
    provider_id = "recording_catalog"

    def __init__(self, package):
        self.package = package
        self.calls = []

    def get_package(self, context, resource_ref):
        self.calls.append((context, resource_ref))
        return self.package


def _skill() -> GeneralSkill:
    return GeneralSkill(
        id="genskill_weather",
        tenant_id="tenant_demo",
        slug="weather",
        name="天气",
        description="查询天气",
        skill_markdown="# Weather",
        skill_files_json=[
            {
                "path": "SKILL.md",
                "content": "# Weather",
                "size": 9,
                "mime_type": "text/markdown",
            }
        ],
        status="published",
    )


def test_provider_package_pin_rejects_content_drift() -> None:
    skill = _skill()
    first = package_from_row(skill)
    skill.skill_markdown = "# Changed"
    second = package_from_row(skill)

    assert first.digest != second.digest
