"""Reference remapping shared by snapshots and offline conversion."""
import re


def remap(value, mapping):
    if isinstance(value, dict): return {key: remap(item, mapping) for key, item in value.items()}
    if isinstance(value, list): return [remap(item, mapping) for item in value]
    if isinstance(value, str):
        if value in mapping: return mapping[value]
        # References occur in paths and colon-delimited related-group IDs.
        # Match ID tokens, without changing longer IDs or remapping replacements.
        changed = {before: after for before, after in mapping.items() if before != after}
        if changed:
            pattern = r"(?<![A-Za-z0-9_-])(?:" + "|".join(re.escape(key) for key in sorted(changed, key=len, reverse=True)) + r")(?![A-Za-z0-9_-])"
            value = re.sub(pattern, lambda match: changed[match.group()], value)
    return value
