from app import paths
from app.core.plan.router import PROMPT_PATH
from app.general_skills.runner import PROMPT_DIR


def test_prompt_path_exists_in_dev() -> None:
    assert PROMPT_PATH.exists()
    assert PROMPT_DIR.exists()


def test_resource_dir_hosts_prompts_and_dist_dir() -> None:
    assert (paths.resource_dir() / "app" / "llm" / "prompts").exists()


def test_mock_server_script_resolves() -> None:
    assert (paths.resource_dir() / "mock_servers" / "mcp_stdio_server.py").exists()
