import base64
import hashlib

from app.core.harness_capability_invoker import HarnessCapabilityInvoker


def test_a2a_file_parts_become_harness_workspace_artifacts(tmp_path) -> None:
    content = b"A2A artifact\n"
    payload = {
        "success": True,
        "data": {
            "artifacts": [
                {
                    "artifactId": "artifact-1",
                    "name": "report.txt",
                    "parts": [
                        {
                            "file": {
                                "name": "report.txt",
                                "mimeType": "text/plain",
                                "bytes": base64.b64encode(content).decode("ascii"),
                            }
                        }
                    ],
                }
            ]
        },
    }
    invoker = object.__new__(HarnessCapabilityInvoker)
    invoker.workspace_root = tmp_path
    invoker.task_frame_id = "task-frame-a2a"

    artifacts = invoker._materialize_a2a_artifacts(payload, call_id="call-a2a")

    assert len(artifacts) == 1
    assert artifacts[0]["display_name"] == "report.txt"
    assert artifacts[0]["sha256"] == hashlib.sha256(content).hexdigest()
    assert (tmp_path / artifacts[0]["path"]).read_bytes() == content
    file_part = payload["data"]["artifacts"][0]["parts"][0]["file"]
    assert "bytes" not in file_part
    assert file_part["path"] == artifacts[0]["path"]
    assert file_part["sandbox_path"].endswith("/report.txt")
