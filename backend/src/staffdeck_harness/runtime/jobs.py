"""Pin an admitted background job to its assembly until it finishes."""
from contextlib import nullcontext


def enqueue_runtime_job(name, func, *args, registry=None, metadata=None, queue_submit=None, **kwargs):
    from app.async_jobs import enqueue_async_job
    lease = registry.work_lease() if registry else nullcontext()
    lease.__enter__()

    def run():
        try:
            return func(*args, **kwargs)
        finally:
            lease.__exit__(None, None, None)

    try:
        return (queue_submit or enqueue_async_job)(name, run, metadata=metadata)
    except BaseException:
        lease.__exit__(None, None, None)
        raise
