from knowledge_core.configuration import Configuration
from knowledge_core.runtime import Runtime as ComposedRuntime


def full_runtime(database_url, **options):
    configuration = Configuration('full', {'authentication': options.get('identity') is not None})
    return ComposedRuntime(database_url, configuration=configuration, **options)
