"""Authentication owns no tables and is independent of space isolation."""
from .base import Plugin
from ..identity import LocalIdentity


def create(runtime):
    if isinstance(runtime.identity, LocalIdentity):
        raise ValueError("Authentication enabled without an identity provider")
    return Plugin(runtime)
