from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.channels.adapters.base import ChannelInbound, get_channel_adapter
from app.db import engine as default_engine
from app.db.models import (
    ChannelBinding,
    ChannelInboundEvent,
    new_id,
    utc_now,
)

logger = logging.getLogger(__name__)

# 批处理限流整形参数(设计文档 §4.3 D3-2):容量 5 / 每 5s 补 1
TOKEN_BUCKET_DEFAULT_CAPACITY = 5
TOKEN_BUCKET_DEFAULT_REFILL_SECONDS = 5.0
TOKEN_BUCKET_DEFAULT_REFILL_AMOUNT = 1
# 回填单次拉取上限(设计文档 §4.4 D4-1):Discord REST 单次上限 100
BACKFILL_DEFAULT_LIMIT = 100


@dataclass
class TokenBucket:
    """每 binding 的令牌桶:容量 capacity,每 refill_period_seconds 补 refill_amount。"""

    capacity: int = TOKEN_BUCKET_DEFAULT_CAPACITY
    refill_period_seconds: float = TOKEN_BUCKET_DEFAULT_REFILL_SECONDS
    refill_amount: int = TOKEN_BUCKET_DEFAULT_REFILL_AMOUNT
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: threading.Lock = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._last_refill
        if elapsed >= self.refill_period_seconds:
            periods = int(elapsed // self.refill_period_seconds)
            self._tokens = min(float(self.capacity), self._tokens + periods * self.refill_amount)
            self._last_refill += periods * self.refill_period_seconds

    def acquire(self) -> bool:
        """非阻塞获取:有令牌即消耗并返回 True,否则 False。"""
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def wait_until_available(self, timeout: float | None = None) -> bool:
        """阻塞直到有令牌可用(限流整形);超时返回 False。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.acquire():
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


@dataclass
class BatchJob:
    """批处理作业(内存首版):投递多分片/多条消息,限流整形后逐条发送。"""

    job_id: str
    binding_id: str
    tenant_id: str
    target: dict[str, Any]
    items: list[Any]
    thread_id: str | None = None
    client_batch_id: str | None = None
    status: str = "pending"  # pending/running/done/failed
    progress: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class BackfillJob:
    """历史回填作业(内存首版):拉取频道历史写入 backfilled 入站事件,不触发 agent。"""

    job_id: str
    binding_id: str
    channel_id: str
    limit: int = BACKFILL_DEFAULT_LIMIT
    after: str | None = None
    before: str | None = None
    status: str = "pending"  # pending/running/done/failed
    written: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)


def _item_parts(item: Any) -> tuple[str, dict[str, Any] | None]:
    """拆解批量项为 (text, payload):纯文本项无 payload,结构化项取 embeds/files。"""
    if isinstance(item, str):
        return item, None
    if isinstance(item, dict):
        text = str(item.get("content") or "")
        payload = {key: value for key, value in item.items() if key != "content" and value}
        return text, payload or None
    raise TypeError(f"不支持的批量项类型: {type(item)!r}")


def _batch_idempotency_key(job_id: str, index: int) -> str:
    """批量项幂等键(设计文档 §4.3 D3-3):batch:{job_id}:{index},失败重试不重复发送。"""
    return f"batch:{job_id}:{index}"


class BatchService:
    """内存批处理/回填作业注册表:submit 入队即起后台线程执行。

    首版不做 DB 持久化,进程重启丢失未完成作业;演进路径:复用 APIJob 状态机
    语义(public_api/jobs.py)迁移到 APIJob 表,幂等键 batch:{job_id}:{index}
    已保证迁移后重试不重复发送。
    """

    def __init__(self) -> None:
        self._batch_jobs: dict[str, BatchJob] = {}
        self._backfill_jobs: dict[str, BackfillJob] = {}
        # (binding_id, client_batch_id) -> job_id:客户端幂等去重
        self._client_batch_keys: dict[tuple[str, str], str] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    # ---------- token 桶 ----------

    def bucket_for(self, binding_id: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(binding_id)
            if bucket is None:
                bucket = TokenBucket()
                self._buckets[binding_id] = bucket
            return bucket

    # ---------- 批处理 ----------

    def submit_batch(
        self,
        binding: ChannelBinding,
        tenant_id: str,
        target: dict[str, Any],
        items: list[Any],
        *,
        thread_id: str | None = None,
        client_batch_id: str | None = None,
        db_engine=None,
        autostart: bool = True,
    ) -> str:
        """入队批量作业并启动后台执行线程,返回 job_id。

        autostart=False 仅供测试直接驱动 run_batch;生产路径保持提交即执行。
        """
        job_id = new_id("chbat")
        with self._lock:
            if client_batch_id:
                existing_id = self._client_batch_keys.get((binding.id, client_batch_id))
                if existing_id and existing_id in self._batch_jobs:
                    return existing_id
            job = BatchJob(
                job_id=job_id,
                binding_id=binding.id,
                tenant_id=tenant_id,
                target=dict(target),
                items=list(items),
                thread_id=thread_id,
                client_batch_id=client_batch_id,
            )
            self._batch_jobs[job_id] = job
            if client_batch_id:
                self._client_batch_keys[(binding.id, client_batch_id)] = job_id
        if not autostart:
            return job_id
        worker = threading.Thread(
            target=self.run_batch,
            args=(job_id,),
            kwargs={"db_engine": db_engine},
            name=f"staffdeck-batch-{job_id}",
            daemon=True,
        )
        worker.start()
        return job_id

    def get_batch(self, job_id: str) -> BatchJob | None:
        with self._lock:
            return self._batch_jobs.get(job_id)

    def run_batch(self, job_id: str, *, db_engine=None) -> None:
        """后台线程主体:逐项限流整形后调用 adapter.send,单条失败不终止整体。"""
        job = self.get_batch(job_id)
        if job is None or job.status == "running":
            return
        job.status = "running"
        try:
            with Session(db_engine or default_engine) as db:
                binding = db.get(ChannelBinding, job.binding_id)
                if binding is None:
                    job.status = "failed"
                    job.errors.append("binding_not_found")
                    return
                db.expunge(binding)
            adapter = get_channel_adapter(binding.channel)
            send = getattr(adapter, "send", None)
            if not callable(send):
                job.status = "failed"
                job.errors.append("adapter_no_send")
                return
            bucket = self.bucket_for(binding.id)
            for index, item in enumerate(job.items):
                try:
                    text, payload = _item_parts(item)
                    target = dict(job.target)
                    if job.thread_id:
                        target["thread_id"] = job.thread_id
                    payload_json = None
                    if payload:
                        payload_json = json.dumps(payload, ensure_ascii=False)
                    if not bucket.wait_until_available():
                        job.errors.append(f"index={index} 限流等待超时")
                        job.failed += 1
                        continue
                    send_kwargs: dict[str, Any] = {
                        "idempotency_key": _batch_idempotency_key(job_id, index),
                    }
                    if payload_json is not None:
                        send_kwargs["payload_json"] = payload_json
                    send(binding, target, text, **send_kwargs)
                    job.succeeded += 1
                except Exception as exc:
                    job.failed += 1
                    job.errors.append(f"index={index}: {str(exc)[:200]}")
                    logger.warning(
                        "批量投递单项失败 job=%s index=%s: %s",
                        job_id,
                        index,
                        exc,
                    )
                finally:
                    job.progress += 1
            job.status = "done"
        except Exception as exc:
            logger.exception("批量作业执行失败 job=%s", job_id)
            job.status = "failed"
            job.errors.append(str(exc)[:500])

    def clear_batches(self, *, completed_only: bool = True) -> int:
        """清理已完成/失败的作业记录;返回清理条数。"""
        with self._lock:
            stale = [
                job_id
                for job_id, job in self._batch_jobs.items()
                if (not completed_only) or job.status in {"done", "failed"}
            ]
            for job_id in stale:
                self._batch_jobs.pop(job_id, None)
            stale_keys = [
                key
                for key, job_id in self._client_batch_keys.items()
                if job_id not in self._batch_jobs
            ]
            for key in stale_keys:
                self._client_batch_keys.pop(key, None)
            return len(stale)

    # ---------- 回填 ----------

    def submit_backfill(
        self,
        binding: ChannelBinding,
        *,
        channel_id: str,
        limit: int = BACKFILL_DEFAULT_LIMIT,
        after: str | None = None,
        before: str | None = None,
        db_engine=None,
        autostart: bool = True,
    ) -> str:
        """入队回填作业并启动后台执行线程,返回 job_id。"""
        job_id = new_id("chbfill")
        job = BackfillJob(
            job_id=job_id,
            binding_id=binding.id,
            channel_id=channel_id,
            limit=limit,
            after=after,
            before=before,
        )
        with self._lock:
            self._backfill_jobs[job_id] = job
        if not autostart:
            return job_id
        worker = threading.Thread(
            target=self.run_backfill,
            args=(job_id,),
            kwargs={"db_engine": db_engine},
            name=f"staffdeck-backfill-{job_id}",
            daemon=True,
        )
        worker.start()
        return job_id

    def get_backfill(self, job_id: str) -> BackfillJob | None:
        with self._lock:
            return self._backfill_jobs.get(job_id)

    def run_backfill(self, job_id: str, *, db_engine=None) -> None:
        """回填执行:拉取历史写入 status=backfilled 事件,幂等查重,不触发 agent。"""
        job = self.get_backfill(job_id)
        if job is None or job.status == "running":
            return
        job.status = "running"
        try:
            with Session(db_engine or default_engine) as db:
                binding = db.get(ChannelBinding, job.binding_id)
                if binding is None:
                    job.status = "failed"
                    job.errors.append("binding_not_found")
                    return
                adapter = get_channel_adapter(binding.channel)
                fetch_history = getattr(adapter, "fetch_history", None)
                if not callable(fetch_history):
                    job.status = "failed"
                    job.errors.append("adapter_no_fetch_history")
                    return
                try:
                    messages = fetch_history(
                        binding,
                        {"channel_id": job.channel_id},
                        before=job.before,
                        after=job.after,
                        limit=job.limit,
                    )
                except Exception as exc:
                    logger.warning("回填拉取历史失败 binding=%s: %s", binding.id, exc)
                    job.status = "failed"
                    job.errors.append(f"fetch_history: {str(exc)[:300]}")
                    return
                if not isinstance(messages, list):
                    job.status = "failed"
                    job.errors.append("fetch_history 返回类型无效")
                    return
                for message in messages:
                    message_dict = _backfill_message_dict(message)
                    event_id = str(message_dict.get("id") or "").strip()
                    if not event_id:
                        continue
                    if job.written + job.duplicates >= job.limit:
                        break
                    target = {
                        "to_user_id": _backfill_author_id(message_dict),
                        "channel_id": str(message_dict.get("channel_id") or job.channel_id),
                        "guild_id": str(message_dict.get("guild_id") or ""),
                        "message_id": event_id,
                    }
                    event = ChannelInboundEvent(
                        id=new_id("chevt"),
                        tenant_id=binding.tenant_id,
                        binding_id=binding.id,
                        channel=binding.channel,
                        event_id=event_id,
                        payload_json={
                            "schema_version": 1,
                            "backfilled": True,
                            "message": message_dict,
                        },
                        config_revision=binding.config_revision,
                        target_json=target,
                        # backfilled 不在 received 集合,durable intake 不消费,
                        # 回填消息天然不触发 agent(设计文档 §4.4 D4-2)
                        status="backfilled",
                    )
                    db.add(event)
                    try:
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                        job.duplicates += 1
                    else:
                        job.written += 1
                job.status = "done"
        except Exception as exc:
            logger.exception("回填作业执行失败 job=%s", job_id)
            job.status = "failed"
            job.errors.append(str(exc)[:500])

    def clear_backfills(self) -> int:
        """清理已完成/失败的回填作业记录;返回清理条数。"""
        with self._lock:
            stale = [
                job_id
                for job_id, job in self._backfill_jobs.items()
                if job.status in {"done", "failed"}
            ]
            for job_id in stale:
                self._backfill_jobs.pop(job_id, None)
            return len(stale)


def _backfill_message_dict(message: Any) -> dict[str, Any]:
    """把 fetch_history 返回统一为可 JSON 序列化的 dict。

    真实 Discord 适配器返回 ChannelInbound(dataclass),测试与未来渠道可能返回
    dict;两种形态在此归一,payload_json 落库只存 dict。
    """
    if isinstance(message, ChannelInbound):
        raw = message.raw if isinstance(message.raw, dict) else {}
        return {
            "id": message.event_id,
            "channel_id": str(raw.get("channel_id") or ""),
            "guild_id": str(raw.get("guild_id") or ""),
            "author_id": message.from_user_id,
            "author": {"id": message.from_user_id},
            "author_name": message.sender_name,
            "content": message.text,
            "created_at": str(raw.get("created_at") or ""),
            "attachments": raw.get("attachments") or [],
        }
    return dict(message)


def _backfill_author_id(message: dict[str, Any]) -> str:
    author = message.get("author")
    if isinstance(author, dict):
        return str(author.get("id") or "")
    return str(message.get("author_id") or "")


# 模块级单例:API 端点与测试共用
batch_service = BatchService()
