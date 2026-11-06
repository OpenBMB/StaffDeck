"""Install-time defaults, never a live-database clone or an upgrade overwrite."""
import json
import os
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


def distribution_root():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    explicit = os.environ.get('STAFFDECK_DISTRIBUTION_MANIFEST')
    if explicit:
        return Path(explicit).expanduser().resolve().parent.parent
    return Path(__file__).resolve().parents[4]


def manifest_path():
    explicit = os.environ.get('STAFFDECK_DISTRIBUTION_MANIFEST')
    return Path(explicit) if explicit else distribution_root() / 'distribution/manifest.json'


def load_manifest():
    path = manifest_path()
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1:
        raise ValueError('Unsupported distribution manifest version')
    return data


def write_once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as stream:
            os.chmod(path, 0o600)
            stream.write(json.dumps(value, ensure_ascii=False, indent=2))
    except FileExistsError:
        if path.is_symlink():
            raise ValueError('Distribution state must not be a symbolic link')


def configure_environment():
    """Called before app.config/database import by desktop/source/Docker launchers."""
    manifest = load_manifest()
    if manifest is None:
        return
    from app.paths import user_data_dir

    from .storage import resolve_storage
    data = user_data_dir()
    storage = resolve_storage(distribution_root(), data, manifest,
                              frozen=bool(getattr(sys, 'frozen', False)))
    os.environ['ULTRARAG_DOTENV'] = storage['dotenv']
    os.environ['APP_SECRET'] = storage['app_secret']
    os.environ['DATABASE_URL'] = storage['database_url']
    os.environ.setdefault('DEMO_SEED_ENABLED', 'false')
    os.environ.setdefault('STAFFDECK_VERSION', manifest.get('version', '0.6.0'))
    config = data / 'assembly.json'
    write_once(config, manifest['assembly'])
    write_once(config.with_suffix('.applied.json'), manifest['assembly'])
    os.environ.setdefault('HARNESS_RUNTIME_CONFIG_PATH', str(config))
    os.environ.setdefault('HARNESS_V3_ENABLED', 'true')
    engine_roots = [distribution_root() / 'harness-engine']
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).resolve()
        engine_roots.append(executable.parent / 'harness-engine')
        if sys.platform == 'darwin':
            engine_roots.append(executable.parent.parent / 'Resources/harness-engine')
    archive_root = None
    if getattr(sys, 'frozen', False) and os.name == 'nt':
        archive = Path(sys.executable).resolve().parent / 'harness-engine.tar.gz'
        if archive.is_file():
            from .runtime_bundle import runtime_path
            archive_root = runtime_path(archive, data)
    engine = archive_root or next((path for path in engine_roots if (path / 'apps/cli/lib/bin.js').is_file()), engine_roots[0])
    os.environ.setdefault('HARNESS_V3_ROOT', str(engine))
    os.environ.setdefault('HARNESS_V3_HOME', str(data / 'engine-home'))
    services = manifest.get('deployment', {}).get('services', [])
    app = next((item for item in services if item['kind'] == 'app'), None)
    if app and len(manifest['deployment']['nodes']) > 1:
        edge = next((item for item in services if item['kind'] == 'edge'), None)
        os.environ.setdefault('ULTRARAG_HOST', '0.0.0.0')
        os.environ.setdefault('ULTRARAG_PORT', str(app['container_port']))
        os.environ.setdefault('STAFFDECK_PUBLIC_URL', (edge or app)['url'])
    control_auth = manifest['installation'].get('control_auth_provider')
    if control_auth:
        os.environ.setdefault('HARNESS_CONTROL_AUTH_PROVIDER', control_auth)
    for name, value in manifest.get('module_configuration', {}).items():
        import re
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*(?:CONFIG|SETTINGS)(?:_FILE|_PATH)?', name):
            raise ValueError('Invalid module configuration environment name')
        target = data / 'module-configs' / (name + '.json')
        write_once(target, value)
        os.environ.setdefault(name, str(target))
    sandboxes = [distribution_root() / 'sandbox']
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).resolve()
        sandboxes.append(executable.parent / 'sandbox')
        if sys.platform == 'darwin':
            sandboxes.append(executable.parent.parent / 'Resources/sandbox')
    for sandbox in sandboxes:
        candidates = (sandbox / 'bin/node', sandbox / 'bin/node.exe',
                      sandbox / 'node/bin/node', sandbox / 'node/node.exe', sandbox / 'node.exe')
        binary = next((path for path in candidates if path.is_file()), None)
        if binary is not None:
            os.environ.setdefault('HARNESS_V3_NODE_BIN', str(binary))
            break


def initialize_defaults(db, manifest=None):
    from sqlmodel import select

    from app.db import models as M
    from app.paths import user_data_dir
    from app.security.auth import hash_password

    from .resources import RESOURCE_MODELS
    manifest = manifest or load_manifest()
    if manifest is None:
        return False
    tenant_id = manifest['installation']['tenant_id']
    if db.get(M.DistributionInstallRecord, tenant_id):
        return True
    # Installing/upgrading onto an existing database must not silently reseed it.
    existing = db.exec(select(M.User).limit(1)).first() or db.exec(select(M.AgentProfile).limit(1)).first()
    marker = M.DistributionInstallRecord(tenant_id=tenant_id, package_id=manifest['package_id'],
                                        status='preserved_existing' if existing else 'initialized')
    if existing:
        if not db.get(M.Tenant, tenant_id):
            raise ValueError('Existing database belongs to another tenant; use a separate data directory')
        db.add(marker)
        db.commit()
        return True
    if manifest['assembly'].get('selections', {}).get('source.staff', 'source.staff.local') != 'source.staff.local':
        raise ValueError('Selected source needs a distribution initializer; local shadow records are forbidden')
    secrets_file = user_data_dir() / 'installation-secrets.json'
    external_auth = manifest['installation'].get('control_auth_provider')
    admin_id = manifest['installation'].get('owner_user_id') or (None if external_auth else 'distribution-admin')
    if not admin_id:
        raise ValueError('External identity default staff require a verified package owner')
    db.add(M.Tenant(id=tenant_id, name=manifest['name']))
    db.add(M.PersonaConfig(tenant_id=tenant_id, system_prompt=(manifest.get('resources') or {}).get('persona_prompt', '')))
    if not external_auth:
        legacy = json.loads(secrets_file.read_text(encoding='utf-8')) if secrets_file.is_file() else {}
        password = os.environ.get('STAFFDECK_INITIAL_ADMIN_PASSWORD') or legacy.get('admin_password') or 'admin'
        db.add(M.User(id=admin_id, tenant_id=tenant_id, username='admin', display_name='Administrator',
                      role='admin', password_hash=hash_password(password)))
    from .defaults import default_resource_graph, default_staff
    staff = manifest['installation']['default_staff']
    records = (manifest.get('resources') or {}).get('records', {})
    if manifest['assembly'].get('security_profile') == 'OSS_LOCAL' and not external_auth:
        defaults = default_staff()
        keys = {row['key'] for row in staff}
        names = {row['name'] for row in staff}
        staff = [row for row in defaults if row['key'] not in keys and row['name'] not in names] + staff
        exported_names = {row['name'] for row in records.get('AgentProfile', [])}
        builtin_names = {row['name'] for row in staff if row in defaults} - exported_names
        builtin = default_resource_graph(builtin_names)['records'] if builtin_names else {}
        # Explicit employee exports take precedence over a matching starter ID.
        records = {name: list({row['id']: row for row in [*builtin.get(name, []),
                                  *records.get(name, [])]}.values())
                   for name in builtin.keys() | records.keys()}
    for name in records:
        if name not in RESOURCE_MODELS:
            raise ValueError('Unknown default resource type')
    for name, model in RESOURCE_MODELS.items():
        for raw in records.get(name, []):
            value = dict(raw)
            value['tenant_id'] = tenant_id
            if 'metadata_json' in value:
                meta = dict(value['metadata_json'] or {})
                for key in ('owner_user_id', 'created_by_user_id', 'creator_user_id', 'gallery_published_by'):
                    if key in meta or key == 'owner_user_id':
                        meta[key] = admin_id
                if not external_auth:
                    for key in ('owner_username', 'created_by_username', 'created_by', 'creator_name'):
                        if key in meta:
                            meta[key] = 'admin'
                value['metadata_json'] = meta
            if name == 'ModelConfig':
                value['api_key_encrypted'] = ''
            db.add(model.model_validate(value))
    imported_names = {row['name'] for row in records.get('AgentProfile', [])}
    for spec in staff:
        if spec['name'] in imported_names:
            continue
        db.add(M.AgentProfile(id='agent_' + uuid5(NAMESPACE_URL, manifest['package_id'] + ':' + spec['key']).hex[:20],
            tenant_id=tenant_id, name=spec['name'].strip(), description=spec['description'],
            persona_prompt=spec['persona_prompt'], harness_max_actions=spec['harness_max_actions'],
            metadata_json={'owner_user_id': admin_id, 'owner_username': 'admin' if not external_auth else '',
                           'created_by_user_id': admin_id, 'created_by_username': 'admin' if not external_auth else '',
                           'role_name': spec['name'].removesuffix('助手'),
                           'distribution_package_id': manifest['package_id']}))
    db.add(marker)
    db.commit()
    return True
