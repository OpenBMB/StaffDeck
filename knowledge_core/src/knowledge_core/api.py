"""Transport selection belongs to the composition root."""
def __getattr__(name):
    if name.startswith("__"): raise AttributeError(name)
    from . import contracts
    return getattr(contracts, name)


def create_app(runtime, *, ui_directory=None):
    if not hasattr(runtime, 'plugins'):
        raise TypeError('Knowledge Core requires an assembled Runtime; use knowledge_core.runtime.Runtime')
    from .kernel.api import create_app as kernel_app
    transport = None
    if runtime.configuration.full_contract:
        from .full_transport import register
        transport = register
    return kernel_app(runtime, ui_directory=ui_directory, transport=transport)
