import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import MCPOAuthCredential, MCPServer, User
from app.tools.mcp_oauth_store import (
    OAuthOwnershipError,
    delete_credentials,
    load_credentials,
    save_credentials,
)


def test_private_encrypted_credentials_survive_restart_and_disconnect(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        for uid, tenant in [('alice', 't'), ('bob', 't'), ('other', 'elsewhere')]:
            db.add(User(id=uid, tenant_id=tenant, username=uid, password_hash='test'))
        db.add(MCPServer(id='s', tenant_id='t', name='test', url='https://example.com/mcp'))
        db.commit()
        save_credentials(db, 't', 'alice', 's', 'https://example.com/mcp',
                         {'access_token': 'private-token', 'refresh_token': 'private-refresh'})
        row = db.exec(select(MCPOAuthCredential)).one()
        assert 'private-token' not in row.encrypted_payload
        assert load_credentials(db, 't', 'bob', 's') is None
        with pytest.raises(OAuthOwnershipError):
            load_credentials(db, 't', 'other', 's')
    with Session(engine) as db:
        assert load_credentials(db, 't', 'alice', 's')['refresh_token'] == 'private-refresh'
        delete_credentials(db, 't', 'alice', 's')
        assert load_credentials(db, 't', 'alice', 's') is None


def test_endpoint_change_does_not_reuse_token(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id='u', tenant_id='t', username='u', password_hash='test'))
        server = MCPServer(id='s', tenant_id='t', name='s', url='https://example.com/mcp')
        db.add(server)
        db.commit()
        save_credentials(db, 't', 'u', 's', server.url, {'access_token': 'secret'})
        server.url = 'https://different.example/mcp'
        db.add(server)
        db.commit()
        with pytest.raises(OAuthOwnershipError):
            load_credentials(db, 't', 'u', 's')
        with pytest.raises(OAuthOwnershipError):
            save_credentials(db, 't', 'u', 's', 'https://example.com/mcp', {})
