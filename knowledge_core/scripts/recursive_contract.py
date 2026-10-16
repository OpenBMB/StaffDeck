"""Compare wire responses without dropping fields, nulls or array ordering."""

from datetime import datetime
import re
import base64
import io
import zipfile
import json

TIMESTAMPS = frozenset({"created_at", "updated_at", "started_at", "finished_at", "expected_updated_at", "timestamp", "online_edited_at", "cancel_requested_at", "cancelled_at"})
GENERATED = re.compile(r"^(?:kb|kbver|kdoc|kbucket|kchunk|kjob|kconcept|kdisc|job|binding|branch|suggestion|publication|agentres|req|apijob)_[A-Za-z0-9_]+$")


class Comparison:
    def __init__(self):
        self.ids = {}
        self.reverse_ids = {}

    def bind(self, expected, actual):
        if expected in self.ids and self.ids[expected] != actual:
            return False
        if actual in self.reverse_ids and self.reverse_ids[actual] != expected:
            return False
        self.ids[expected] = actual
        self.reverse_ids[actual] = expected
        return True

    def compare(self, expected, actual, path="$", field=""):
        differences = []
        if type(expected) is not type(actual):
            return [{"path": path, "reason": "type", "expected": expected, "actual": actual}]
        if isinstance(expected, dict):
            for key in sorted(expected.keys() | actual.keys()):
                next_path = path + "." + key
                if key not in expected or key not in actual:
                    differences.append({"path": next_path, "reason": "field_presence",
                                        "expected_present": key in expected, "actual_present": key in actual})
                else:
                    differences.extend(self.compare(expected[key], actual[key], next_path, key))
        elif isinstance(expected, list):
            if len(expected) != len(actual):
                differences.append({"path": path, "reason": "array_length", "expected": len(expected), "actual": len(actual)})
            for index, (left, right) in enumerate(zip(expected, actual)):
                differences.extend(self.compare(left, right, f"{path}[{index}]", field))
        elif isinstance(expected, str):
            if field == "content_base64":
                try:
                    def archive(value):
                        with zipfile.ZipFile(io.BytesIO(base64.b64decode(value, validate=True))) as bundle:
                            return {name: bundle.read(name).decode() for name in sorted(bundle.namelist())}
                    return self.compare(archive(expected), archive(actual), path + ".archive")
                except (ValueError, zipfile.BadZipFile, UnicodeError):
                    pass
            if field == "content_md" or field.endswith(".md"):
                def markdown(value):
                    if not value.startswith("---\n"): return {"body": value}
                    header, body = value[4:].split("\n---\n", 1)
                    parsed = {}
                    for line in header.splitlines():
                        key, separator, text = line.partition(":")
                        if not separator: return {"body": value}
                        try: parsed[key] = json.loads(text.strip())
                        except ValueError: parsed[key] = text.strip()
                    return {"frontmatter": parsed, "body": body}
                left, right = markdown(expected), markdown(actual)
                # Compare the body as ordinary text, keeping all content and links.
                return self.compare(left, right, path + ".markdown")
            if field in TIMESTAMPS:
                try:
                    datetime.fromisoformat(expected.replace("Z", "+00:00"))
                    datetime.fromisoformat(actual.replace("Z", "+00:00"))
                    return []
                except ValueError:
                    pass
            if expected in self.ids:
                equal = self.ids[expected] == actual
            elif GENERATED.fullmatch(expected) and GENERATED.fullmatch(actual):
                equal = self.bind(expected, actual)
            else:
                mapped = expected
                for old, new in sorted(self.ids.items(), key=lambda item: len(item[0]), reverse=True):
                    mapped = mapped.replace(old, new)
                equal = mapped == actual
            if not equal:
                differences.append({"path": path, "reason": "value", "expected": expected, "actual": actual})
        elif expected != actual:
            differences.append({"path": path, "reason": "value", "expected": expected, "actual": actual})
        return differences


def compare_reports(baseline, current):
    comparison = Comparison()
    expected = {row["case"]: row for row in baseline["cases"]}
    observed = {row["case"]: row for row in current["cases"]}
    rows = []
    for name in list(expected) + [key for key in observed if key not in expected]:
        left, right = expected.get(name), observed.get(name)
        differences = []
        if left is None or right is None:
            differences = [{"path": "$", "reason": "missing_case"}]
        elif left.get("execution_error") or right.get("execution_error"):
            differences = [{"path": "$", "reason": "execution_error", "expected": left.get("execution_error"), "actual": right.get("execution_error")}]
        else:
            # Establish IDs from matching structural positions before checking
            # embedded citations, including IDs defined later in an object.
            def bind_ids(a, b, field=""):
                if isinstance(a, dict) and isinstance(b, dict):
                    for key in a.keys() & b.keys():
                        bind_ids(a[key], b[key], key)
                elif isinstance(a, list) and isinstance(b, list):
                    for x, y in zip(a, b): bind_ids(x, y, field)
                elif (field == "id" or field.endswith("_id")) and isinstance(a, str) and isinstance(b, str) and GENERATED.fullmatch(a) and GENERATED.fullmatch(b):
                    comparison.bind(a, b)
            bind_ids(left["response"], right["response"])
            differences = comparison.compare(left["response"], right["response"])
        rows.append({"case": name, "baseline": baseline["source_commit"],
                     "request": right.get("request") if right else None,
                     "expected_request": left.get("request") if left else None,
                     "expected": left.get("response") if left else None,
                     "actual": right.get("response") if right else None,
                     "status": "failed" if differences else "passed", "differences": differences})
    return rows
