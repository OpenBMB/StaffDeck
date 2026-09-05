from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

ELEMENT_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "audit_elements"
    / "energy_management.json"
)

MANAGEMENT_SYSTEM_ELEMENT_KEYS = {
    "能源管理体系": "GB/T 23331-2020",
}


class AuditElement(BaseModel):
    id: str
    management_system: str
    title: str
    requirement: str
    query_templates: list[str] = Field(min_length=1)
    report_section_id: str
    required: bool = True


def load_required_elements(management_systems: list[str]) -> list[AuditElement]:
    selected = {
        MANAGEMENT_SYSTEM_ELEMENT_KEYS.get(system, system)
        for system in management_systems
    }
    rows = json.loads(ELEMENT_FIXTURE.read_text(encoding="utf-8"))
    result = [
        AuditElement.model_validate(row)
        for row in rows
        if row["management_system"] in selected
    ]
    by_id = {row.id: row for row in result}
    if len(by_id) != len(result):
        raise ValueError("duplicate audit element id")
    return [by_id[key] for key in sorted(by_id)]
