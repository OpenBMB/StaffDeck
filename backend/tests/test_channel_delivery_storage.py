from types import SimpleNamespace as NS
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from fastapi import HTTPException
from app.channels.delivery_days import delivery_day_bucket, server_timezone
from app.db.models import ChannelDelivery


@pytest.mark.parametrize('dialect', ['sqlite','postgresql'])
def test_uses_actual_delivery_model_binding_not_default_database(monkeypatch,dialect):
    monkeypatch.setenv('TZ','Asia/Shanghai')
    seen=[]
    db=NS(get_bind=lambda **kw: seen.append(kw) or NS(dialect=NS(name=dialect)))
    expression=delivery_day_bucket(db)
    compiler=postgresql.dialect() if dialect=='postgresql' else sqlite.dialect()
    sql=str(select(expression).compile(dialect=compiler,compile_kwargs={'literal_binds':True}))
    assert seen==[{'mapper':ChannelDelivery}]
    if dialect=='postgresql':
        assert "timezone('Asia/Shanghai', timezone('UTC'," in sql and ' AS DATE)' in sql
        assert 'localtime' not in sql
    else:
        assert "'localtime'" in sql


def test_invalid_timezone_is_not_silently_replaced(monkeypatch):
    monkeypatch.setenv('TZ','invalid/timezone')
    with pytest.raises(HTTPException) as error:server_timezone()
    assert error.value.status_code==503


def test_named_timezone_supports_dst(monkeypatch):
    monkeypatch.setenv('TZ','America/New_York')
    assert server_timezone()=='America/New_York'
