"""Keep an existing local installation's database, encryption key and files in place."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote

from cryptography.fernet import Fernet, InvalidToken
from dotenv import dotenv_values

LEGACY_APP_SECRET = 'change-me-in-development'


def sqlite_path(url: str, base: Path) -> Path | None:
    if not url.startswith('sqlite:///') or url == 'sqlite:///:memory:':
        return None
    path = Path(unquote(url.removeprefix('sqlite:///'))).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _check_key(path: Path, secret: str) -> None:
    """Read only; reject the wrong key before touching installation state."""
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Known historical credential columns; never execute identifiers from database contents.
        for table, column in (('model_configs', 'api_key_encrypted'),
                              ('channel_accounts', 'secret_encrypted')):
            if table not in tables:
                continue
            columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            if column not in columns:
                continue
            for (value,) in db.execute(f'SELECT {column} FROM {table} WHERE {column} IS NOT NULL'):
                if value:
                    try:
                        Fernet(key).decrypt(value.encode())
                    except (InvalidToken, ValueError) as exc:
                        raise RuntimeError('原数据库的 APP_SECRET 不匹配；请恢复原配置，升级不会重置模型密钥。') from exc


def _backup(path: Path, data: Path, version: str) -> None:
    name = hashlib.sha256((str(path) + ':' + version).encode()).hexdigest()[:16]
    target = data / 'backups' / f'pre-upgrade-{name}.db'
    if target.is_symlink():
        raise RuntimeError('数据库备份位置不能是符号链接。')
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.' + secrets.token_hex(4) + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        # sqlite3's connection context only commits/rolls back; it does not close
        # the handle. Windows requires both handles closed before atomic rename.
        with (closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source,
              closing(sqlite3.connect(temporary)) as backup):
            source.backup(backup)
            if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('数据库备份校验失败，已停止升级。')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _installation_material(data: Path, *, create: bool = False) -> str | None:
    path=data/'.installation-seed'
    if path.is_symlink():raise RuntimeError('安装签名材料不能是符号链接。')
    if not path.exists() and create:
        try:
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd,'wb') as output:output.write(os.urandom(32))
    if not path.exists():return None
    material=path.read_bytes()
    if len(material)!=32:raise RuntimeError('安装签名材料损坏；请恢复原数据目录。')
    return hashlib.sha256(material).hexdigest()


def resolve_storage(root: Path, data: Path, manifest: dict, *, frozen: bool = False) -> dict:
    explicit_data = bool(os.environ.get('ULTRARAG_DATA_DIR'))
    dotenv = Path(os.environ.get('ULTRARAG_DOTENV') or data / '.env')
    if (not os.environ.get('ULTRARAG_DOTENV') and not dotenv.exists()
            and not explicit_data and not frozen and (root / 'backend/.env').is_file()):
        dotenv = root / 'backend/.env'
    values = dotenv_values(dotenv) if dotenv.is_file() else {}
    stored_file = data / 'installation-secrets.json'
    if stored_file.is_symlink():
        raise RuntimeError('安装密钥文件不能是符号链接。')
    stored = json.loads(stored_file.read_text(encoding='utf-8')) if stored_file.is_file() else {}
    base = data if frozen else root / 'backend'
    url = os.environ.get('DATABASE_URL') or values.get('DATABASE_URL')
    if not url:
        candidates = [data / 'skill_agent_loop.db']
        if not explicit_data and not frozen:
            candidates.append(root / 'backend/skill_agent_loop.db')
        existing = list(dict.fromkeys(p.resolve() for p in candidates if p.is_file()))
        if len(existing) > 1:
            raise RuntimeError('检测到多个旧数据库；请用 DATABASE_URL 指定要升级的数据库。')
        if not existing and any(data.glob('*.db')):
            raise RuntimeError('数据目录已有数据库；请用 DATABASE_URL 指定原数据库，避免创建空库。')
        url = 'sqlite:///' + str(existing[0] if existing else data / 'skill_agent_loop.db')
    path = sqlite_path(url, base)
    if path:
        # Relative .env URLs keep the legacy backend-relative meaning, independent of cwd.
        url = 'sqlite:///' + str(path)
    secret = (os.environ.get('APP_SECRET') or values.get('APP_SECRET') or stored.get('app_secret')
              or _installation_material(data))
    if path and path.is_file():
        secret = secret or LEGACY_APP_SECRET
        _check_key(path, secret)
        _backup(path, data, manifest.get('version', 'upgrade'))
    secret = secret or _installation_material(data,create=True)
    return {'database_url': url, 'app_secret': secret, 'dotenv': str(dotenv)}
