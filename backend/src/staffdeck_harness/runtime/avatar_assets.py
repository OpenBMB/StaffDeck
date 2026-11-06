"""Bounded avatar byte cache. Every download still authenticates and checks Staff view."""
import base64
import hashlib
import threading
import time
from collections import OrderedDict
from urllib.parse import quote

_items = OrderedDict()
_lock = threading.Lock()
_bytes = 0
LIMIT = 16 * 1024 * 1024


def realm(db):
    from staffdeck_harness.modules.registry import peek_registry
    from staffdeck_harness.contracts.manifest import SlotName
    registry = db.info.get('staffdeck_registry') or peek_registry()
    source = registry.provider(SlotName.STAFF_SOURCE) if registry else None
    services = db.info.get('staffdeck_runtime_services')
    return (getattr(services, 'namespace', 'local'), source.manifest.module_id if source else 'local', getattr(registry, 'generation', 0))


def remember(tenant_id, staff_id, value, *, namespace=('local','local',0)):
    global _bytes
    if not isinstance(value, str) or not value.startswith('data:image/') or len(value) > 8 * 1024 * 1024:
        return None
    try:
        header, encoded = value.split(',', 1)
        mime = header[5:].split(';')[0]
        if mime not in {'image/png','image/jpeg','image/webp','image/gif'} or not header.endswith(';base64'):
            return None
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    digest = hashlib.sha256(data).hexdigest()
    key = (namespace, tenant_id, staff_id, digest)
    with _lock:
        old = _items.pop(key, None)
        if old: _bytes -= len(old[0])
        _items[key] = (data, mime, time.monotonic()+600)
        _bytes += len(data)
        while _bytes > LIMIT or len(_items) > 256:
            _, item = _items.popitem(last=False)
            _bytes -= len(item[0])
    return f'/api/chat/agents/{quote(staff_id, safe="")}/avatar/{digest}?tenant_id={quote(tenant_id, safe="")}'


def read(tenant_id, staff_id, digest, *, namespace=('local','local',0)):
    global _bytes
    with _lock:
        key = (namespace, tenant_id, staff_id, digest)
        item = _items.get(key)
        if item and item[2] > time.monotonic():
            _items.move_to_end(key)
            return item[:2]
        if item:
            _bytes -= len(_items.pop(key)[0])
    return None
