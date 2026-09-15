"""Each API/service test owns its process-level module assembly."""
import pytest


@pytest.fixture(autouse=True)
def isolated_module_assembly():
    from staffdeck_harness.modules.registry import reset_registry
    from staffdeck_harness.modules.config import reset_env_snapshot
    from staffdeck_harness.security.profile import reset_profile
    reset_registry()
    reset_profile()
    reset_env_snapshot()
    yield
    reset_registry()
    reset_profile()
    reset_env_snapshot()
