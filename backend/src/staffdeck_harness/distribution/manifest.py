"""A portable assembly snapshot with explicit install-time secret requirements."""
from dataclasses import asdict
from datetime import datetime, timezone
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

SECRET = re.compile(r'(?:^|_)(?:password|password_hash|secret|api_key|access_key|private_key|access_token|refresh_token|decision_token|control_token|authorization|credential|credentials|cookie|token|encrypted)$', re.I)
ASSEMBLY_FIELDS = {'engine', 'security_profile', 'disabled_modules', 'enabled_modules',
                   'extra_modules', 'selections', 'module_configs', 'placements', 'base', 'ui_variant'}


def portable(value, path, required):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', key).replace('-', '_')
            location = f'{path}.{key}'
            if key in {'env_json', 'env'} and isinstance(item, dict):
                result[key] = {name: '' for name in item}
                required.extend(f'{location}.{name}' for name, value in item.items() if value)
            elif isinstance(item, str) and normalized in {'path', 'cwd', 'command', 'storage_path', 'workspace_root', 'harness_storage_path'} and (item.startswith('/') or re.match(r'^[A-Za-z]:[\\/]', item)):
                required.append(location)
                result[key] = ''
            elif SECRET.search(normalized):
                if item:
                    required.append(location)
                result[key] = ''
            elif key not in {'last_test_ok', 'last_test_at', 'updated_by', 'updated_at'}:
                result[key] = portable(item, location, required)
        return result
    if isinstance(value, list):
        return [portable(item, f'{path}[{i}]', required) for i, item in enumerate(value)]
    if isinstance(value, str) and '://' in value:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            required.append(path)
            return ''
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if any(SECRET.search(key.replace('-', '_')) for key, _ in query):
            required.append(path)
            return urlunsplit(parsed._replace(query=urlencode([(key, '' if SECRET.search(key.replace('-', '_')) else val) for key, val in query])))
    return value


def make_manifest(request, overrides, settings=None):
    raw = asdict(overrides)
    if settings is not None:
        raw['base'] = asdict(overrides.base.effective(settings))
    required = []
    assembly = portable({key: value for key, value in raw.items() if key in ASSEMBLY_FIELDS}, 'assembly', required)
    assembly['engine'] = 'harness_v3'
    control_auth = str(getattr(settings, 'harness_control_auth_provider', '') or '')
    staff = [row.model_dump() for row in request.default_staff]
    if (assembly.get('security_profile') == 'OSS_LOCAL' and not control_auth
            and assembly.get('selections', {}).get('source.staff', 'source.staff.local') == 'source.staff.local'):
        from .defaults import default_staff
        keys = {row['key'] for row in staff}
        names = {row['name'] for row in staff}
        staff = [row for row in default_staff() if row['key'] not in keys and row['name'] not in names] + staff
    return {
        'schema_version': 1, 'package_id': uuid4().hex,
        'name': request.name, 'version': request.version, 'kind': request.kind,
        'platform': request.platform, 'configuration': request.configuration,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'assembly': assembly,
        'product_features': {'framework_management': False, 'enterprise_admin': True},
        'installation': {'tenant_id': request.tenant_id,
                         'control_auth_provider': control_auth,
                         'default_staff': staff},
        'employee_ids': list(request.employee_ids),
        'deployment': {'nodes': [row.model_dump() for row in request.deployment_nodes],
                       'services': [row.model_dump() for row in request.services]},
        'required_configuration': sorted(set(required)),
        'exclusions': ['credentials', 'databases', 'chat_history', 'runtime_workspaces', 'model_weights', 'framework_management'],
    }
