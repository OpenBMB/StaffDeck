"""Bound deterministic capability failures without retrying external side effects."""

import json


class CapabilityRecovery:
    RESOLUTION_ERRORS = frozenset({
        "TOOL_NOT_AVAILABLE", "CAPABILITY_NOT_AVAILABLE", "CAPABILITY_NOT_ACTIVATED",
        "UNSUPPORTED_CAPABILITY", "ACTIVATION_FENCED", "NOT_FOUND", "DISABLED", "NOT_ALLOWED",
        "PERMISSION_DENIED", "PRE_TOOL_DENIED", "CAPABILITY_AUTHORIZATION_REVOKED",
        "REQUIRED_CAPABILITY_NOT_INVOKED",
        "CAPABILITY_SNAPSHOT_CHANGED",
    })
    ARGUMENT_ERRORS = frozenset({"INVALID_ARGUMENTS", "VALIDATION_ERROR", "INVALID_TRANSITION",
                                "REQUIRED_SLOT_MISSING", "NON_RETRYABLE_ACTION_REPEATED"})

    def __init__(self):
        self.counts = {}

    def observe(self, name, arguments, result):
        if result.get("success") is True:
            self.counts = {key: count for key, count in self.counts.items() if key[0] != name}
            return None
        error = result.get("error") or {}
        if not isinstance(error, dict):
            return None
        code = str(error.get("code") or "")
        if code not in self.RESOLUTION_ERRORS | self.ARGUMENT_ERRORS:
            return None
        # Varying parameters cannot fix an unknown/unbound resource. Schema errors, however,
        # allow corrected parameters. Unrelated session/describe reads do not reset this count.
        args = "" if code in self.RESOLUTION_ERRORS else json.dumps(arguments, sort_keys=True, default=str)
        key = (name, code, args)
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] < 3:
            return None
        return {"code": "CAPABILITY_RECOVERY_EXHAUSTED", "retryable": False,
                "message": "当前步骤的能力调用连续受阻，已暂停自动重试。请检查能力绑定或参数后继续。",
                "details": {"tool_name": name, "cause_code": code, "attempts": self.counts[key]}}
