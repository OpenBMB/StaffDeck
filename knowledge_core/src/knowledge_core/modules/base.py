from sqlalchemy import MetaData


class Plugin:
    schema_version = 1
    operations = {}

    def __init__(self, runtime):
        self.runtime = runtime
        self.metadata = MetaData()

    def prepare(self, filename, text, specs, context, model, config): return None
    def process(self, db, document_id, text, buckets, chunks, context, prepared=None): pass
    def retrieve(self, result, value, context): pass
    def snapshot(self, db, document_id): return None
    def restore_content(self, db, value, document_id, mapping): pass
    def content_changed(self, db, document_id): pass
    def register(self, app, dependencies): pass
    def start(self): pass
    def close(self): pass
