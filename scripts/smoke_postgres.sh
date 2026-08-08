#!/usr/bin/env bash
# Postgres/高斯冒烟:DATABASE_URL=postgresql+psycopg://... 下验证
# create_all、/api/health 与日分桶/知识库/渠道配置三处方言路径。
# 用法:DATABASE_URL='postgresql+psycopg://user:pass@host:5432/dbname' scripts/smoke_postgres.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/backend/.venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="${PYTHON:-python3}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "请先设置 DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname" >&2
  exit 1
fi
if [[ "$DATABASE_URL" != postgresql* ]]; then
  echo "DATABASE_URL 必须是 postgresql(+psycopg) 方言: $DATABASE_URL" >&2
  exit 1
fi

echo "== 1/4 create_all(schema 初始化) =="
(cd "$ROOT_DIR/backend" && DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" - <<'PY'
from app.db.database import init_db

init_db()
print("create_all ok")
PY
)

echo "== 2/4 日分桶/知识库/渠道配置方言路径 =="
(cd "$ROOT_DIR/backend" && DATABASE_URL="$DATABASE_URL" "$PYTHON_BIN" - <<'PY'
"""三处生产方言点冒烟:全部走 ORM/方言助手,失败即抛异常。"""
from sqlmodel import Session, select

from app.api.channels import _patch_binding_config_key
from app.api.knowledge import _safe_bucket_chunk_rows, _safe_document_bucket_rows
from app.db import engine
from app.db.dialect import get_dialect
from app.db.models import (
    ChannelBinding,
    ChannelDelivery,
    KnowledgeBucket,
    KnowledgeChunk,
    utc_now,
)

backend = engine.url.get_backend_name()
dialect = get_dialect(backend)
print(f"backend={backend} dialect={dialect.name}")

# advisory lock 闭环:acquire → check → release(真实 PG 验证会话级锁与 pg_locks 校验)
from sqlalchemy import text as sa_text

with Session(engine) as lock_session:
    assert dialect.acquire_advisory_lock(lock_session, "smoke-connector"), "acquire 失败"
    assert dialect.check_advisory_lock(lock_session, "smoke-connector"), "check 未持锁"
    # 回归:制造并发池流量(其他连接频繁 checkout)后,锁校验必须仍 True——
    # 专属 Connection 不归池,pg_backend_pid 稳定;Session.commit 方案在此会误判
    for _ in range(3):
        with engine.connect() as busy_conn:
            busy_conn.execute(sa_text("SELECT 1"))
        with Session(engine) as busy_session:
            busy_session.execute(sa_text("SELECT 1"))
            busy_session.commit()
    assert dialect.check_advisory_lock(lock_session, "smoke-connector"), "并发 checkout 后锁校验误判"
    dialect.release_advisory_lock(lock_session, "smoke-connector")
    assert not dialect.check_advisory_lock(lock_session, "smoke-connector"), "release 后仍持锁"
print("advisory lock roundtrip ok")

# 部分唯一索引存在(PG 方言按方言 DDL 创建,不进 metadata)
from sqlalchemy import inspect as sa_inspect

index_names = {i["name"] for i in sa_inspect(engine).get_indexes("model_configs")}
assert "uq_model_configs_tenant_default" in index_names, index_names
print("partial index present ok")

with Session(engine) as db:
    binding = ChannelBinding(
        tenant_id="smoke_tenant",
        agent_id="agent_smoke",
        channel="wecom",
        status="active",
        config_json={"corp_id": "corpSmoke", "bot_id": "bot_smoke"},
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)

    # 渠道配置补丁:ORM 读-改-写(原 json_set 路径)
    _patch_binding_config_key(db, binding.tenant_id, binding.id, "auto_route", False)
    db.commit()
    db.refresh(binding)
    assert binding.config_json.get("auto_route") is False, binding.config_json
    assert get_dialect(backend).json_config_get(binding.config_json, "corp_id") == "corpSmoke"

    # 日分桶:方言助手表达式可执行且分组正确
    db.add(
        ChannelDelivery(
            tenant_id="smoke_tenant",
            binding_id=binding.id,
            session_id="s_smoke",
            kind="reply",
            text="smoke",
            status="delivered",
            next_attempt_at=utc_now(),
            idempotency_key="smoke_day_bucket",
        )
    )
    bucket = KnowledgeBucket(
        tenant_id="smoke_tenant",
        knowledge_base_id="kb_smoke",
        knowledge_base_version_id="kbv_smoke",
        document_id="doc_smoke",
        bucket_key="bucket_smoke",
        title="冒烟片段",
        summary="smoke",
    )
    db.add(bucket)
    db.flush()
    db.add(
        KnowledgeChunk(
            tenant_id="smoke_tenant",
            knowledge_base_id="kb_smoke",
            knowledge_base_version_id="kbv_smoke",
            document_id="doc_smoke",
            bucket_id=bucket.id,
            chunk_index=0,
            content="smoke chunk",
        )
    )
    db.commit()

    day_bucket = dialect.day_bucket(ChannelDelivery.created_at)
    day_rows = db.exec(
        select(day_bucket)
        .where(ChannelDelivery.binding_id == binding.id)
        .group_by(day_bucket)
    ).all()
    assert len(day_rows) == 1, day_rows

    # 知识库安全读(非 SQLite 走 ORM 分支)
    bucket_rows = _safe_document_bucket_rows(db, "smoke_tenant", "doc_smoke")
    assert len(bucket_rows) == 1 and bucket_rows[0]["title"] == "冒烟片段"
    chunk_rows = _safe_bucket_chunk_rows(db, "smoke_tenant", bucket.id)
    assert len(chunk_rows) == 1 and chunk_rows[0]["content"] == "smoke chunk"

    # 清理冒烟数据
    db.delete(binding)
    db.delete(bucket)
    db.commit()
print("dialect paths ok")
PY
)

echo "== 3/4 应用启动 + /api/health =="
UVICORN_PORT="${SMOKE_PORT:-58099}"
(cd "$ROOT_DIR/backend" && DATABASE_URL="$DATABASE_URL" \
  "$PYTHON_BIN" -m uvicorn app.main:app --port "$UVICORN_PORT" >/tmp/smoke_pg_uvicorn.log 2>&1 &
  echo $! > /tmp/smoke_pg_uvicorn.pid)
trap 'kill "$(cat /tmp/smoke_pg_uvicorn.pid)" 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${UVICORN_PORT}/api/health" >/dev/null 2>&1; then
    echo "/api/health ok"
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:${UVICORN_PORT}/api/health"

echo "== 4/4 冒烟完成 =="
