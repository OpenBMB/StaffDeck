import pytest
from fastapi import HTTPException
from test_general_skills import _admin_user, _seed_minimal_tenant, _test_session

from app.api.general_skills import _validate_skill_package_references, import_general_skill
from app.general_skills.schema import GeneralSkillFile, GeneralSkillImportRequest


@pytest.mark.parametrize(
    "reference",
    [
        "references/gongwen_rules.yaml",
        "references/gongwen_rules.yaml (GB/T 9704-2012)",
        "references/gongwen_rules.yaml\uff08GB/T 9704-2012\uff09",
        "\uff08references/gongwen_rules.yaml\uff09",
        "[rules](references/gongwen_rules.yaml)\uff08GB/T 9704-2012\uff09",
        "`references/gongwen_rules.yaml`\uff08GB/T 9704-2012\uff09",
        "./references/gongwen_rules.yaml\uff08GB/T 9704-2012\uff09",
        "references/gongwen_rules.yaml#rules\uff08GB/T 9704-2012\uff09",
        "references/gongwen_rules.yaml?version=1\uff08GB/T 9704-2012\uff09",
    ],
)
@pytest.mark.parametrize("file_present", [True, False])
def test_reference_annotations_do_not_become_file_paths(reference: str, file_present: bool) -> None:
    files = [GeneralSkillFile(path="SKILL.md", content=reference)]
    if file_present:
        files.append(GeneralSkillFile(path="references/gongwen_rules.yaml", content="rules: {}"))
        _validate_skill_package_references(files)
    else:
        with pytest.raises(HTTPException) as error:
            _validate_skill_package_references(files)
        assert error.value.status_code == 400
        assert error.value.detail == (
            "General skill package is missing files referenced by SKILL.md: "
            "references/gongwen_rules.yaml"
        )


@pytest.mark.parametrize(
    ("reference", "path"),
    [
        ("references/rules(v2).yaml", "references/rules(v2).yaml"),
        ("[rules](references/rules(v2).yaml)", "references/rules(v2).yaml"),
        ("`references/rules\uff08v2\uff09.yaml`", "references/rules\uff08v2\uff09.yaml"),
        ("`references/rules(v2)`", "references/rules(v2)"),
        ("references/rules%EF%BC%88v2%EF%BC%89.yaml", "references/rules\uff08v2\uff09.yaml"),
    ],
)
def test_reference_file_name_punctuation_is_preserved(reference: str, path: str) -> None:
    files = [
        GeneralSkillFile(path="SKILL.md", content=reference),
        GeneralSkillFile(path=path, content="rules: {}"),
    ]
    _validate_skill_package_references(files)


def test_quoted_missing_reference_does_not_match_existing_prefix() -> None:
    files = [
        GeneralSkillFile(path="SKILL.md", content="`references/rules\uff08v2\uff09.yaml`"),
        GeneralSkillFile(path="references/rules", content="rules: {}"),
    ]
    with pytest.raises(HTTPException) as error:
        _validate_skill_package_references(files)
    assert error.value.detail.endswith("references/rules\uff08v2\uff09.yaml")


@pytest.mark.parametrize(
    "reference",
    [
        "references/gongwen_rules.yaml\uff08GB/T 9704-2012\uff09",
        "[rules](references/gongwen_rules.yaml)\uff08GB/T 9704-2012\uff09",
    ],
)
def test_import_general_skill_accepts_reference_with_annotation(reference: str) -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        created = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="WPS Office Suite",
                slug="wps-office-suite",
                files=[
                    {"path": "SKILL.md", "content": f"# WPS Office Suite\n{reference}\n"},
                    {"path": "references/gongwen_rules.yaml", "content": "rules: {}\n"},
                ],
            ),
            db,
            _admin_user(),
        )
        assert created.slug == "wps-office-suite"
        assert [file.path for file in created.skill_files] == [
            "SKILL.md",
            "references/gongwen_rules.yaml",
        ]
