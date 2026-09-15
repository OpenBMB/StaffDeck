"""Day bucketing follows the channel model's actual storage and server timezone."""
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import Date, cast, func


def server_timezone():
    candidates = []
    configured = os.environ.get('TZ', '').lstrip(':')
    if configured:
        candidates.append(configured)
    else:
        try:
            resolved = str(Path('/etc/localtime').resolve())
            if '/zoneinfo/' in resolved:
                candidates.append(resolved.split('/zoneinfo/', 1)[1])
        except OSError:
            pass
        try:
            candidates.append(Path('/etc/timezone').read_text().strip())
        except OSError:
            pass
        if not time.daylight and set(time.tzname) <= {'UTC', 'GMT'}:
            candidates.append('UTC')
    for name in candidates:
        if '/zoneinfo/' in name:
            name = name.split('/zoneinfo/', 1)[1]
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            continue
    raise HTTPException(503, {'code':'SERVER_TIMEZONE_UNAVAILABLE',
                             'message':'无法确定投递日志时区，请配置服务器 TZ 为 IANA 时区'})


def delivery_day_bucket(db):
    from app.db.models import ChannelDelivery
    # A session can bind control data to SQLite and delivery rows to PostgreSQL.
    # Do not inspect the default engine or branch on an edition name.
    dialect = db.get_bind(mapper=ChannelDelivery).dialect.name
    if dialect == 'sqlite':
        return func.date(ChannelDelivery.created_at, 'localtime')
    if dialect == 'postgresql':
        # Runtime timestamps are UTC-naive; convert UTC -> named server timezone
        # before taking the date, including historical DST offsets.
        return cast(func.timezone(server_timezone(), func.timezone('UTC', ChannelDelivery.created_at)), Date)
    raise HTTPException(503, {'code':'STORAGE_DIALECT_UNSUPPORTED',
                             'message':'当前存储不支持投递日志日期分组'})
