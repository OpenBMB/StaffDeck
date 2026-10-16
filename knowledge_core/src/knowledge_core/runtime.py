"""Select a configured composition without importing disabled implementations."""
from .configuration import Configuration


class Runtime:
    def __new__(cls, database_url, *, configuration=None, **options):
        configuration = configuration or Configuration()
        from .kernel.runtime import KernelRuntime
        from .assembly import assemble
        options["plugin_factory"] = assemble
        return KernelRuntime(database_url, configuration=configuration, **options)
