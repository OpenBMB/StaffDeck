"""A process lock serializes runtime ownership and offline replacement."""
from pathlib import Path
import fcntl
from sqlalchemy.engine import make_url


class DatabaseLock:
    def __init__(self, database_url, *, recovery=False):
        self.file = None
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:": return
        path = Path(url.database).absolute()
        if not recovery and path.with_name(path.name + ".km-conversion.json").exists():
            raise ValueError("Offline replacement was interrupted; run --conversion recover before startup")
        self.file = path.with_name(path.name + ".km-lock").open("a+")
        try: fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.file.close(); self.file = None
            raise ValueError("Stop the KM service before offline conversion or another instance startup") from error

    def close(self):
        if self.file:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close(); self.file = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
