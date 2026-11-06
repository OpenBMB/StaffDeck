"""Interpret legacy field descriptors without modifying immutable SOP definitions."""
from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from typing import Any


def slot_field_names(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise TypeError("SOP field declarations must be a list")
    names = []
    for value in values:
        text = str(value).strip()
        descriptor = value if isinstance(value, Mapping) else None
        if descriptor is None and text.startswith('{') and text.endswith('}') and len(text) <= 4096:
            try:
                descriptor = json.loads(text)
            except (ValueError, RecursionError):
                try:
                    descriptor = ast.literal_eval(text)
                except (ValueError, SyntaxError, TypeError, RecursionError):
                    descriptor = None
        field = descriptor.get('field') if isinstance(descriptor, Mapping) else None
        # Unrecognized descriptors remain required literal fields; never drop a constraint.
        name = field.strip() if isinstance(field, str) and field.strip() else text
        if name and name not in names:
            names.append(name)
    return names
