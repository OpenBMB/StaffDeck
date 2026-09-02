"""Module Registry and built-in module set. Every pluggable thing registers here."""

from staffdeck_dsh.modules.registry import (
    ENTRY_POINT_GROUP,
    SUPPORTED_CONTRACTS,
    DuplicateModule,
    Installed,
    ModuleRegistry,
    RegistrySealed,
    SlotConflict,
    UnsatisfiedRequirement,
    discover_and_install,
    get_registry,
    manifest,
    reset_registry,
)

__all__ = [
    "ENTRY_POINT_GROUP", "SUPPORTED_CONTRACTS", "DuplicateModule", "Installed", "ModuleRegistry", "RegistrySealed",
    "SlotConflict", "UnsatisfiedRequirement", "discover_and_install", "get_registry", "manifest", "reset_registry",
]
