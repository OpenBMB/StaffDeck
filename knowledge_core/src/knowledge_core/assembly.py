"""The composition root is the only place that imports optional factories."""
from dataclasses import dataclass
from importlib import import_module
from .configuration import DEPENDENCIES, MODULE_NAMES


@dataclass(frozen=True)
class ModuleDescriptor:
    name: str
    factory: str
    dependencies: tuple[str, ...] = ()
    schema_version: int = 1
    tables: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()
    lifecycle: tuple[str, ...] = ("register", "start", "close")

    def load(self, runtime):
        module, factory = self.factory.rsplit(":", 1)
        plugin = getattr(import_module(module), factory)(runtime)
        configured = set(plugin.metadata.tables)
        expected = set(self.tables)
        if self.name == "spaces":
            if runtime.configuration.enabled("catalog"): expected.add("km_catalog_space_bindings")
            if runtime.configuration.enabled("versions"): expected.update(("km_branch_spaces", "km_version_spaces"))
        if configured != expected or plugin.schema_version != self.schema_version:
            raise ValueError(f"Module {self.name} storage declaration differs from its factory")
        if any(not callable(getattr(plugin, method, None)) for method in self.lifecycle):
            raise ValueError(f"Module {self.name} lifecycle declaration differs from its factory")
        return plugin


STORAGE = {
    "authentication": (), "spaces": ("km_spaces", "km_space_contents"),
    "catalog": ("km_catalogs", "km_catalog_contents"),
    "versions": ("km_versions", "km_branches", "km_branch_contents"),
    "concepts": ("km_concepts", "km_concept_contexts"), "discovery": ("km_suggestions", "km_suggestion_contexts"), "publication": ("km_publications",),
}
INTERFACES = {"authentication": ("identity",), "spaces": ("space_directory", "bindings"),
    "catalog": ("catalog",), "versions": ("branches", "snapshots"), "concepts": ("concepts", "okf"),
    "discovery": ("suggestions",), "publication": ("publication",)}
REGISTRY = {name: ModuleDescriptor(name, f"knowledge_core.modules.{name}:create", DEPENDENCIES.get(name, ()),
                                  3 if name == "spaces" else 2 if name in {"catalog", "versions", "concepts", "discovery"} else 1,
                                  STORAGE[name], INTERFACES[name]) for name in MODULE_NAMES}


def assemble(runtime):
    result = {}
    runtime.plugins = result
    for name, descriptor in REGISTRY.items():
        if runtime.configuration.enabled(name):
            result[name] = descriptor.load(runtime)
    return result
